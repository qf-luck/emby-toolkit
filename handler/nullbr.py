# handler/nullbr.py
import logging
import requests
import re
import time  
import threading 
from datetime import datetime
from database import settings_db, media_db, request_db
import config_manager

import constants
import utils
import handler.tmdb as tmdb
try:
    from p115client import P115Client
except ImportError:
    P115Client = None

logger = logging.getLogger(__name__)

# ★★★ 硬编码配置：Nullbr ★★★
NULLBR_APP_ID = "7DqRtfNX3"
NULLBR_API_BASE = "https://api.nullbr.com"

# 内存缓存，用于存储用户等级以控制请求频率，避免每次都查库
_user_level_cache = {
    "sub_name": "free",
    "daily_used": 0,
    "daily_quota": 0,
    "updated_at": 0
}

def get_config():
    return settings_db.get_setting('nullbr_config') or {}

def _get_headers():
    config = get_config()
    api_key = config.get('api_key')
    headers = {
        "Content-Type": "application/json",
        "X-APP-ID": NULLBR_APP_ID,
        "User-Agent": f"EmbyToolkit/{constants.APP_VERSION}"
    }
    if api_key:
        headers["X-API-KEY"] = api_key
    return headers

def _parse_size_to_gb(size_str):
    """将大小字符串转换为 GB (float)"""
    if not size_str: return 0.0
    size_str = size_str.upper().replace(',', '')
    match = re.search(r'([\d\.]+)\s*(TB|GB|MB|KB)', size_str)
    if not match: return 0.0
    num = float(match.group(1))
    unit = match.group(2)
    if unit == 'TB': return num * 1024
    elif unit == 'GB': return num
    elif unit == 'MB': return num / 1024
    elif unit == 'KB': return num / 1024 / 1024
    return 0.0

def _is_resource_valid(item, filters, media_type='movie', episode_count=0):
    """根据配置过滤资源 (保持原有逻辑)"""
    if not filters: return True
    
    # 1. 分辨率
    if filters.get('resolutions'):
        res = item.get('resolution')
        if not res or res not in filters['resolutions']: return False

    # 2. 质量
    if filters.get('qualities'):
        item_quality = item.get('quality')
        if not item_quality: return False
        q_list = [item_quality] if isinstance(item_quality, str) else item_quality
        if not any(q in q_list for q in filters['qualities']): return False

    # 3. 大小过滤 (GB) 
    if media_type == 'tv':
        min_size = float(filters.get('tv_min_size') or filters.get('min_size') or 0)
        max_size = float(filters.get('tv_max_size') or filters.get('max_size') or 0)
    else:
        min_size = float(filters.get('movie_min_size') or filters.get('min_size') or 0)
        max_size = float(filters.get('movie_max_size') or filters.get('max_size') or 0)
    
    if min_size > 0 or max_size > 0:
        size_gb = _parse_size_to_gb(item.get('size'))
        
        # 如果是剧集且获取到了集数，计算单集平均大小
        check_size = size_gb
        if media_type == 'tv' and episode_count > 0:
            check_size = size_gb / episode_count
            # 简单的防误判：如果计算出的单集太小（比如小于0.1G），可能是获取到了单集资源而不是整季包
            # 但这里我们主要目的是过滤整季包，所以按平均值算没问题。
            # 如果资源本身就是单集（如S01E01），除以总集数后会变得非常小，自然会被 min_size 过滤掉，
            # 这正好符合“订阅整季”的需求（不要单集散件）。

        if min_size > 0 and check_size < min_size:
            return False
        if max_size > 0 and check_size > max_size:
            return False

    # 4. 中字
    if filters.get('require_zh'):
        if item.get('is_zh_sub'): return True
        title = item.get('title', '').upper()
        zh_keywords = ['中字', '中英', '字幕', 'CHS', 'CHT', 'CN', 'DIY', '国语', '国粤']
        if not any(k in title for k in zh_keywords): return False

    # 5. 容器 (仅电影)
    if media_type != 'tv' and filters.get('containers'):
        title = item.get('title', '').lower()
        link = item.get('link', '').lower()
        ext = None
        if 'mkv' in title or link.endswith('.mkv'): ext = 'mkv'
        elif 'mp4' in title or link.endswith('.mp4'): ext = 'mp4'
        elif 'iso' in title or link.endswith('.iso'): ext = 'iso'
        if not ext or ext not in filters['containers']: return False

    return True

# ==============================================================================
# ★★★ 新增：用户 API 交互与自动流控 ★★★
# ==============================================================================

def get_user_info():
    """获取用户信息"""
    url = f"{NULLBR_API_BASE}/user/info"
    try:
        proxies = config_manager.get_proxies_for_requests()
        response = requests.get(url, headers=_get_headers(), timeout=15, proxies=proxies)
        response.raise_for_status()
        data = response.json()
        
        if data.get('success'):
            user_data = data.get('data', {})
            _user_level_cache.update({
                'sub_name': user_data.get('sub_name', 'free').lower(),
                'daily_used': user_data.get('daily_used', 0),
                'daily_quota': user_data.get('daily_quota', 0),
                'updated_at': time.time()
            })
            return user_data
        else:
            raise Exception(data.get('message', '获取用户信息失败'))
    except Exception as e:
        logger.error(f"  ⚠️ 获取 NULLBR 用户信息异常: {e}")
        raise e

def redeem_code(code):
    """
    使用兑换码
    """
    url = f"{NULLBR_API_BASE}/user/redeem"
    payload = {"code": code}
    try:
        proxies = config_manager.get_proxies_for_requests()
        
        response = requests.post(url, json=payload, headers=_get_headers(), timeout=15, proxies=proxies)
        data = response.json()
        
        if response.status_code == 200 and data.get('success'):
            get_user_info()
            return data
        else:
            msg = data.get('message') or "兑换失败"
            return {"success": False, "message": msg}
    except Exception as e:
        logger.error(f"  ➜ 兑换请求异常: {e}")
        return {"success": False, "message": str(e)}

def _wait_for_rate_limit():
    """
    根据用户等级自动执行流控睡眠
    Free: 25 req/min -> ~2.4s interval
    Silver: 60 req/min -> ~1.0s interval
    Golden: 100 req/min -> ~0.6s interval
    """
    # 如果缓存过期(超过1小时)，尝试更新一下，但不阻塞主流程
    if time.time() - _user_level_cache['updated_at'] > 3600:
        try:
            get_user_info()
        except:
            pass 

    level = _user_level_cache.get('sub_name', 'free')
    
    if 'golden' in level:
        time.sleep(0.6)
    elif 'silver' in level:
        time.sleep(1.0)
    else:
        # Free or unknown
        time.sleep(2.5)

def _enrich_items_with_status(items):
    """批量查询本地库状态 (保持不变)"""
    if not items: return items
    tmdb_ids = [str(i.get('tmdbid') or i.get('id')) for i in items if (i.get('tmdbid') or i.get('id'))]
    if not tmdb_ids: return items

    library_map_movie = media_db.check_tmdb_ids_in_library(tmdb_ids, 'Movie')
    library_map_series = media_db.check_tmdb_ids_in_library(tmdb_ids, 'Series')
    sub_status_movie = request_db.get_global_subscription_statuses_by_tmdb_ids(tmdb_ids, 'Movie')
    sub_status_series = request_db.get_global_subscription_statuses_by_tmdb_ids(tmdb_ids, 'Series')

    for item in items:
        tid = str(item.get('tmdbid') or item.get('id') or '')
        mtype = item.get('media_type', 'movie')
        if not tid: continue
        
        in_lib = False
        sub_stat = None
        if mtype == 'tv':
            if f"{tid}_Series" in library_map_series: in_lib = True
            sub_stat = sub_status_series.get(tid)
        else:
            if f"{tid}_Movie" in library_map_movie: in_lib = True
            sub_stat = sub_status_movie.get(tid)
        
        item['in_library'] = in_lib
        item['subscription_status'] = sub_stat
    return items

def get_preset_lists():
    custom_presets = settings_db.get_setting('nullbr_presets')
    if custom_presets and isinstance(custom_presets, list) and len(custom_presets) > 0:
        return custom_presets
    return utils.DEFAULT_NULLBR_PRESETS

def fetch_list_items(list_id, page=1):
    _wait_for_rate_limit()
    url = f"{NULLBR_API_BASE}/list/{list_id}"
    params = {"page": page}
    try:
        proxies = config_manager.get_proxies_for_requests()
        response = requests.get(url, params=params, headers=_get_headers(), timeout=15, proxies=proxies)
        response.raise_for_status()
        data = response.json()
        items = data.get('items', [])
        enriched_items = _enrich_items_with_status(items)
        return {"code": 200, "data": {"list": enriched_items, "total": data.get('total_results', 0)}}
    except Exception as e:
        logger.error(f"获取片单失败: {e}")
        raise e

def search_media(keyword, page=1):
    _wait_for_rate_limit() # 自动流控
    url = f"{NULLBR_API_BASE}/search"
    params = { "query": keyword, "page": page }
    try:
        proxies = config_manager.get_proxies_for_requests()
        response = requests.get(url, params=params, headers=_get_headers(), timeout=15, proxies=proxies)
        response.raise_for_status()
        data = response.json()
        items = data.get('items', [])
        enriched_items = _enrich_items_with_status(items)
        return { "code": 200, "data": { "list": enriched_items, "total": data.get('total_results', 0) } }
    except Exception as e:
        logger.error(f"  ➜ NULLBR 搜索失败: {e}")
        raise e

def _fetch_single_source(tmdb_id, media_type, source_type, season_number=None):
    _wait_for_rate_limit() # 自动流控
    
    url = ""
    if media_type == 'movie':
        url = f"{NULLBR_API_BASE}/movie/{tmdb_id}/{source_type}"
    elif media_type == 'tv':
        if season_number:
            url = f"{NULLBR_API_BASE}/tv/{tmdb_id}/season/{season_number}/{source_type}"
        else:
            if source_type == '115':
                url = f"{NULLBR_API_BASE}/tv/{tmdb_id}/115"
            elif source_type == 'magnet':
                url = f"{NULLBR_API_BASE}/tv/{tmdb_id}/season/1/magnet"
            else:
                return []
    else:
        return []

    try:
        proxies = config_manager.get_proxies_for_requests()
        response = requests.get(url, headers=_get_headers(), timeout=10, proxies=proxies)
        
        if response.status_code == 404: return []
        
        if response.status_code == 402:
            logger.warning("  ⚠️ NULLBR 接口返回 402: 配额已耗尽")
            if _user_level_cache['daily_quota'] > 0:
                _user_level_cache['daily_used'] = _user_level_cache['daily_quota']
            return []
            
        response.raise_for_status()
        
        _user_level_cache['daily_used'] = _user_level_cache.get('daily_used', 0) + 1
        
        data = response.json()
        raw_list = data.get(source_type, [])
        
        cleaned_list = []
        for item in raw_list:
            link = item.get('share_link') or item.get('magnet') or item.get('ed2k')
            title = item.get('title') or item.get('name')
            
            if link and title:
                if media_type == 'tv' and source_type == 'magnet' and not season_number:
                    title = f"[S1] {title}"
                
                is_zh = item.get('zh_sub') == 1
                if not is_zh:
                    t_upper = title.upper()
                    zh_keywords = ['中字', '中英', '字幕', 'CHS', 'CHT', 'CN', 'DIY', '国语', '国粤']
                    if any(k in t_upper for k in zh_keywords): is_zh = True
                
                # 季号清洗逻辑 (保持不变)
                if media_type == 'tv' and season_number:
                    try:
                        target_season = int(season_number)
                        match = re.search(r'(?:^|\.|\[|\s|-)S(\d{1,2})(?:\.|\]|\s|E|-|$)', title.upper())
                        if match and int(match.group(1)) != target_season: continue
                        match_zh = re.search(r'第(\d{1,2})季', title)
                        if match_zh and int(match_zh.group(1)) != target_season: continue
                    except: pass

                cleaned_list.append({
                    "title": title,
                    "size": item.get('size', '未知'),
                    "resolution": item.get('resolution'),
                    "quality": item.get('quality'),
                    "link": link,
                    "source_type": source_type.upper(),
                    "is_zh_sub": is_zh
                })
        return cleaned_list
    except Exception as e:
        logger.warning(f"  ➜ 获取 {source_type} 资源失败: {e}")
        return []

def fetch_resource_list(tmdb_id, media_type='movie', specific_source=None, season_number=None):
    config = get_config()
    if specific_source:
        sources_to_fetch = [specific_source]
    else:
        sources_to_fetch = config.get('enabled_sources', ['115', 'magnet', 'ed2k'])

    if _user_level_cache.get('daily_quota', 0) > 0 and _user_level_cache.get('daily_used', 0) >= _user_level_cache.get('daily_quota', 0):
        logger.warning(f"  ⚠️ 今日 API 配额已用完 ({_user_level_cache['daily_used']}/{_user_level_cache['daily_quota']})，跳过请求")
        raise Exception("今日 API 配额已用完，请明日再试或升级套餐。")
    
    all_resources = []
    
    if '115' in sources_to_fetch:
        try: all_resources.extend(_fetch_single_source(tmdb_id, media_type, '115', season_number))
        except: pass
    if 'magnet' in sources_to_fetch:
        try: all_resources.extend(_fetch_single_source(tmdb_id, media_type, 'magnet', season_number))
        except: pass
    if media_type == 'movie' and 'ed2k' in sources_to_fetch:
        try: all_resources.extend(_fetch_single_source(tmdb_id, media_type, 'ed2k'))
        except: pass
    
    filters = config.get('filters', {})
    # 如果 filters 全为空值，则不过滤
    has_filter = any(filters.values())
    if not has_filter:
        return all_resources
        
    # 如果是剧集，获取该季的总集数，用于后续按单集平均大小过滤
    episode_count = 0
    if media_type == 'tv' and season_number:
        try:
            tmdb_api_key = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_TMDB_API_KEY)
            if tmdb_api_key:
                season_info = tmdb.get_tv_season_details(tmdb_id, season_number, tmdb_api_key)
                if season_info and 'episodes' in season_info:
                    episode_count = len(season_info['episodes'])
                    logger.info(f"  ➜ [NULLBR] 获取到 第 {season_number} 季 总集数: {episode_count}，将按单集平均大小过滤。")
        except Exception as e:
            logger.warning(f"  ⚠️ 获取 TMDb 季集数失败，将按总大小过滤: {e}")

    # 5. 执行过滤 (传入 episode_count)
    filtered_list = [
        res for res in all_resources 
        if _is_resource_valid(res, filters, media_type, episode_count=episode_count)
    ]
    logger.info(f"  ➜ 资源过滤: 原始 {len(all_resources)} -> 过滤后 {len(filtered_list)}")
    return filtered_list

# ==============================================================================
# ★★★ 115 推送逻辑  ★★★
# ==============================================================================

def _clean_link(link):
    """
    清洗链接：去除首尾空格，并安全去除末尾的 HTML 脏字符 (&#)
    """
    if not link:
        return ""
    link = link.strip()
    while link.endswith('&#') or link.endswith('&') or link.endswith('#'):
        if link.endswith('&#'):
            link = link[:-2]
        elif link.endswith('&') or link.endswith('#'):
            link = link[:-1]
    return link

def notify_cms_scan():
    """
    通知 CMS 执行目录整理 (生成 strm)
    接口: /api/sync/lift_by_token?type=auto_organize&token=...
    """
    config = get_config()
    cms_url = config.get('cms_url')
    cms_token = config.get('cms_token')

    if not cms_url or not cms_token:
        # 用户没配置 CMS，直接忽略，不报错
        return

    cms_url = cms_url.rstrip('/')
    # 构造通知接口 URL
    api_url = f"{cms_url}/api/sync/lift_by_token"
    params = {
        "type": "auto_organize",
        "token": cms_token
    }

    try:
        logger.info(f"  ➜ 正在通知 CMS 执行整理...")
        response = requests.get(api_url, params=params, timeout=5)
        response.raise_for_status()
        
        res_json = response.json()
        if res_json.get('code') == 200 or res_json.get('success'):
            logger.info(f"  ✅ CMS 通知成功: {res_json.get('msg', 'OK')}")
        else:
            logger.warning(f"  ⚠️ CMS 通知返回异常: {res_json}")

    except Exception as e:
        logger.warning(f"  ⚠️ CMS 通知发送失败: {e}")
        raise e

def push_to_115(resource_link, title):
    """
    智能推送：支持 115/115cdn/anxia 转存 和 磁力离线
    """
    if P115Client is None:
        raise ImportError("未安装 p115 库")

    config = get_config()
    cookies = config.get('p115_cookies')
    
    try:
        cid_val = config.get('p115_save_path_cid', 0)
        save_path_cid = int(cid_val) if cid_val else 0
    except:
        save_path_cid = 0

    if not cookies:
        raise ValueError("未配置 115 Cookies")

    clean_url = _clean_link(resource_link)
    logger.info(f"  ➜ [NULLBR] 待处理链接: {clean_url}")
    
    client = P115Client(cookies)
    
    try:
        # 支持 115.com, 115cdn.com, anxia.com
        target_domains = ['115.com', '115cdn.com', 'anxia.com']
        is_115_share = any(d in clean_url for d in target_domains) and ('magnet' not in clean_url)
        
        if is_115_share:
            logger.info(f"  ➜ [NULLBR] 识别为 115 转存任务 -> CID: {save_path_cid}")
            share_code = None
            match = re.search(r'/s/([a-z0-9]+)', clean_url)
            if match: share_code = match.group(1)
            if not share_code: raise Exception("无法从链接中提取分享码")
            receive_code = ''
            pwd_match = re.search(r'password=([a-z0-9]+)', clean_url)
            if pwd_match: receive_code = pwd_match.group(1)
            
            resp = {} 
            try:
                if hasattr(client, 'fs_share_import_to_dir'):
                     resp = client.fs_share_import_to_dir(share_code, receive_code, save_path_cid)
                elif hasattr(client, 'fs_share_import'):
                    resp = client.fs_share_import(share_code, receive_code, save_path_cid)
                elif hasattr(client, 'share_import'):
                    resp = client.share_import(share_code, receive_code, save_path_cid)
                else:
                    api_url = "https://webapi.115.com/share/receive"
                    payload = {'share_code': share_code, 'receive_code': receive_code, 'cid': save_path_cid}
                    r = client.request(api_url, method='POST', data=payload)
                    resp = r.json() if hasattr(r, 'json') else r
            except Exception as e:
                raise Exception(f"调用转存接口失败: {e}")

            if resp and resp.get('state'):
                logger.info(f"  ✅ 115 转存成功: {title}")
                return True
            else:
                err = resp.get('error_msg') if resp else '无响应'
                err = err or resp.get('msg') or str(resp)
                raise Exception(f"转存失败: {err}")

        else:
            # ==================================================
            # ★★★ 磁力/Ed2k 离线下载 (指纹对比版) ★★★
            # ==================================================
            logger.info(f"  ➜ [NULLBR] 识别为磁力/离线任务 -> CID: {save_path_cid}")
            
            # 1. 【关键步骤】建立快照：记录当前目录下已存在文件的 pick_code
            existing_pick_codes = set()
            try:
                # 获取前50个文件/文件夹 (按上传时间倒序)
                # 注意：115 API 返回的 pc (pick_code) 是唯一标识
                files_res = client.fs_files({'cid': save_path_cid, 'limit': 50, 'o': 'user_ptime', 'asc': 0})
                if files_res.get('data'):
                    for item in files_res['data']:
                        if item.get('pc'):
                            existing_pick_codes.add(item.get('pc'))
            except Exception as e:
                logger.warning(f"  ⚠️ 获取目录快照失败(可能是空目录): {e}")
            
            logger.info(f"  ➜ [NULLBR] 当前目录已有 {len(existing_pick_codes)} 个项目")

            # 2. 添加任务
            payload = {'url[0]': clean_url, 'wp_path_id': save_path_cid}
            resp = client.offline_add_urls(payload)
            
            if resp.get('state'):
                # 获取 info_hash 用于辅助检查死链
                result_list = resp.get('result', [])
                info_hash = None
                if result_list and isinstance(result_list, list):
                    info_hash = result_list[0].get('info_hash')

                # 3. 轮询检测目录 (延长到 45秒)
                # 文件夹生成比较慢，给足时间
                max_retries = 3  # 15次 * 3秒 = 45秒
                success_found = False
                
                logger.info(f"  ➜ [NULLBR] 任务已提交，正在扫描新项目...")

                for i in range(max_retries):
                    time.sleep(3) 
                    
                    # --- A. 检查目录是否有【不在快照里】的新项目 ---
                    try:
                        check_res = client.fs_files({'cid': save_path_cid, 'limit': 50, 'o': 'user_ptime', 'asc': 0})
                        if check_res.get('data'):
                            for item in check_res['data']:
                                current_pc = item.get('pc')
                                # 如果发现一个 pick_code 不在旧集合里，说明是新生成的
                                if current_pc and (current_pc not in existing_pick_codes):
                                    item_name = item.get('n', '未知')
                                    logger.info(f"  ✅ [第{i+1}次检查] 发现新项目: {item_name}")
                                    success_found = True
                                    break
                        if success_found:
                            break
                    except Exception as e:
                        pass # 网络波动忽略

                    # --- B. 辅助检查：任务是否挂了 ---
                    try:
                        list_resp = client.offline_list(page=1)
                        tasks = list_resp.get('tasks', [])
                        for task in tasks[:10]:
                            if info_hash and task.get('info_hash') == info_hash:
                                if task.get('status') == -1:
                                    try: client.offline_delete([task.get('info_hash')])
                                    except: pass
                                    raise Exception("115任务状态变为[下载失败]")
                    except Exception as task_err:
                        if "下载失败" in str(task_err): raise task_err
                        pass

                if success_found:
                    logger.info(f"  ✅ [NULLBR] 115 离线成功: {title}")
                    return True
                else:
                    # 超时未发现新文件
                    try: 
                        if info_hash: client.offline_delete([info_hash])
                    except: pass
                    
                    logger.warning(f"  [NULLBR] ❌ 未在目录发现新项目，判定为死链")
                    raise Exception("资源无效，请换个源试试")

            else:
                err = resp.get('error_msg') or resp.get('msg') or '未知错误'
                if '已存在' in str(err):
                    logger.info(f"  ✅ 任务已存在: {title}")
                    return True
                raise Exception(f"离线失败: {err}")

    except Exception as e:
        logger.error(f"  ➜ 115 推送异常: {e}")
        if "Login" in str(e) or "cookie" in str(e).lower():
            raise Exception("115 Cookie 无效")
        raise e

def get_115_account_info():
    """
    极简状态检查：只验证 Cookie 是否有效，不获取任何详情
    """
    if P115Client is None:
        raise Exception("未安装 p115client")
        
    config = get_config()
    cookies = config.get('p115_cookies')
    
    if not cookies:
        raise Exception("未配置 Cookies")
        
    try:
        client = P115Client(cookies)
        
        # 尝试列出 1 个文件，这是验证 Cookie 最快最准的方法
        resp = client.fs_files({'limit': 1})
        
        if not resp.get('state'):
            raise Exception("Cookie 已失效")
            
        # 只要没报错，就是有效
        return {
            "valid": True,
            "msg": "Cookie 状态正常，可正常推送"
        }

    except Exception as e:
        raise Exception("Cookie 无效或网络不通")

def handle_push_request(link, title):
    """
    统一推送入口
    """
    # 1. 推送到 115 (如果失败或死链，这里会直接抛出异常，中断流程)
    push_to_115(link, title)
    
    # 2. 115 成功后，通知 CMS 整理
    notify_cms_scan()
    
    return True

def auto_download_best_resource(tmdb_id, media_type, title, season_number=None):
    """
    [自动任务专用] 搜索并下载最佳资源
    :param season_number: 季号 (仅 media_type='tv' 时有效)
    """
    try:
        config = get_config()
        if not config.get('api_key'):
            logger.warning("NULLBR 未配置 API Key，无法执行自动兜底。")
            return False

        priority_sources = ['115', 'magnet', 'ed2k']
        user_enabled = config.get('enabled_sources', priority_sources)
        
        # 构造日志标题
        log_title = title
        if media_type == 'tv' and season_number:
            log_title = f"《{title}》第 {season_number} 季"

        logger.info(f"  ➜ [NULLBR] 开始搜索资源: {log_title} (ID: {tmdb_id})")

        for source in priority_sources:
            if source not in user_enabled: continue
            if media_type == 'tv' and source == 'ed2k': continue

            resources = fetch_resource_list(tmdb_id, media_type, specific_source=source, season_number=season_number)
            
            if not resources:
                continue

            logger.info(f"  ➜ [{source.upper()}] 找到 {len(resources)} 个资源，开始尝试推送...")

            for index, res in enumerate(resources):
                try:
                    logger.info(f"  👉 尝试第 {index + 1} 个资源: {res['title']}")
                    
                    # 调用统一推送入口 (115 -> CMS Notify)
                    handle_push_request(res['link'], title)
                    
                    logger.info(f"  ✅ 资源推送成功，停止后续尝试。")
                    return True
                    
                except Exception as e:
                    logger.warning(f"  ❌ 第 {index + 1} 个资源推送失败: {e}")
                    logger.info("  🔄 正在尝试下一个资源...")
                    continue
            
            logger.info(f"  ⚠️ [{source.upper()}] 所有资源均尝试失败，切换下一源...")

        logger.info(f"  ❌ 所有源的所有资源均尝试失败: {log_title}")
        return False

    except Exception as e:
        logger.error(f"  ➜ NULLBR 自动兜底失败: {e}")
        return False