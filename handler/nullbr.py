# handler/nullbr.py
import logging
import requests
import re
import time  
import os 
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
    """根据配置过滤资源"""
    if not filters:
        return True

    # 1. 分辨率过滤
    allowed_resolutions = filters.get('resolutions', [])
    if allowed_resolutions:
        res = item.get('resolution')
        if not res or res not in allowed_resolutions:
            logger.debug(f"  ➜ 资源《{item.get('title')}》被过滤掉了，因为分辨率 {res} 不在允许列表中")
            return False

    # 2. 质量过滤
    allowed_qualities = filters.get('qualities', [])
    if allowed_qualities:
        item_quality = item.get('quality')
        if not item_quality: return False
        q_list = [item_quality] if isinstance(item_quality, str) else item_quality
        if not any(q in q_list for q in allowed_qualities): 
            logger.debug(f"  ➜ 资源《{item.get('title')}》被过滤掉了，因为质量 {item_quality} 不在允许列表中")
            return False

    # 3. 大小过滤 (GB) 
    min_size = 0.0
    max_size = 0.0

    if media_type == 'tv':
        # 优先取 tv_min_size，取不到(None)则尝试取 min_size，最后默认为 0
        v_min = filters.get('tv_min_size')
        if v_min is None: v_min = filters.get('min_size')
        min_size = float(v_min or 0)

        v_max = filters.get('tv_max_size')
        if v_max is None: v_max = filters.get('max_size')
        max_size = float(v_max or 0)
    else:
        v_min = filters.get('movie_min_size')
        if v_min is None: v_min = filters.get('min_size')
        min_size = float(v_min or 0)

        v_max = filters.get('movie_max_size')
        if v_max is None: v_max = filters.get('max_size')
        max_size = float(v_max or 0)
    
    if min_size > 0 or max_size > 0:
        size_gb = _parse_size_to_gb(item.get('size'))
        
        # 计算检查用的数值
        check_size = size_gb
        
        # 只有当是剧集、且成功获取到了集数、且集数大于0时，才计算平均大小
        if media_type == 'tv' and episode_count > 0:
            check_size = size_gb / episode_count
            # 调试日志 (可选开启)
            # logger.debug(f"  [大小检查] 总大小: {size_gb}G, 集数: {episode_count}, 平均: {check_size:.2f}G (限制: {min_size}-{max_size})")

        if min_size > 0 and check_size < min_size:
            logger.debug(f"  ➜ 资源《{item.get('title')}》被过滤掉了，因为大小 {check_size:.2f}G 小于最小限制 {min_size}G")
            return False
        if max_size > 0 and check_size > max_size:
            logger.debug(f"  ➜ 资源《{item.get('title')}》被过滤掉了，因为大小 {check_size:.2f}G 大于最大限制 {max_size}G")
            return False

    # 4. 中字过滤
    if filters.get('require_zh'):
        if item.get('is_zh_sub'): return True
        title = item.get('title', '').upper()
        zh_keywords = ['中字', '中英', '字幕', 'CHS', 'CHT', 'CN', 'DIY', '国语', '国粤']
        if not any(k in title for k in zh_keywords): 
            logger.debug(f"  ➜ 资源《{item.get('title')}》被过滤掉了，因为未检测到中文字幕")
            return False
            

    # 5. 容器过滤
    allowed_containers = filters.get('containers', [])
    if allowed_containers:
        if media_type == 'tv': return True
        title = item.get('title', '').lower()
        link = item.get('link', '').lower()
        ext = None

        if link.startswith('ed2k://'):
            # Ed2k 格式: ed2k://|file|文件名|大小|哈希|/
            # 使用 | 分割，文件名通常在第 3 部分 (索引 2)
            try:
                parts = link.split('|')
                if len(parts) >= 3:
                    file_name_in_link = parts[2].lower()
                    if file_name_in_link.endswith('.mkv'): ext = 'mkv'
                    elif file_name_in_link.endswith('.mp4'): ext = 'mp4'
                    elif file_name_in_link.endswith('.iso'): ext = 'iso'
                    elif file_name_in_link.endswith('.ts'): ext = 'ts'
                    elif file_name_in_link.endswith('.avi'): ext = 'avi'
            except:
                pass # 解析失败则忽略，回退到下方逻辑

        # 如果上面没提取到 (比如是磁力链或 115 码)，则走原有逻辑
        if not ext:
            if 'mkv' in title or link.endswith('.mkv'): ext = 'mkv'
            elif 'mp4' in title or link.endswith('.mp4'): ext = 'mp4'
            elif 'iso' in title or link.endswith('.iso'): ext = 'iso'
            elif 'ts' in title or link.endswith('.ts'): ext = 'ts'
            elif 'avi' in title or link.endswith('.avi'): ext = 'avi'
            
        if not ext or ext not in allowed_containers: 
            logger.debug(f"  ➜ 资源《{item.get('title')}》被过滤掉了，因为容器 {ext} 不在允许列表中")
            return False

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

def _fetch_single_source(tmdb_id, media_type, source_type, season_number=None, episode_number=None):
    _wait_for_rate_limit() # 自动流控
    
    url = ""
    if media_type == 'movie':
        url = f"{NULLBR_API_BASE}/movie/{tmdb_id}/{source_type}"
    elif media_type == 'tv':
        # ★★★ 核心修改：支持单集 URL 拼接 ★★★
        if season_number is not None:
            if episode_number is not None:
                # 接口: /tv/{id}/season/{s}/episode/{e}/{source}
                url = f"{NULLBR_API_BASE}/tv/{tmdb_id}/season/{season_number}/episode/{episode_number}/{source_type}"
            else:
                # 接口: /tv/{id}/season/{s}/{source}
                url = f"{NULLBR_API_BASE}/tv/{tmdb_id}/season/{season_number}/{source_type}"
        else:
            # 整剧搜索 (通常只有 115 支持，或者 magnet 搜第一季)
            if source_type == '115':
                url = f"{NULLBR_API_BASE}/tv/{tmdb_id}/115"
            elif source_type == 'magnet':
                # 如果没传季号，默认搜第1季磁力，或者你可以选择不搜
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
                
                # 季号清洗逻辑
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

def fetch_resource_list(tmdb_id, media_type='movie', specific_source=None, season_number=None, episode_number=None):
    config = get_config()
    
    # 1. 确定要搜索的源
    if specific_source:
        sources_to_fetch = [specific_source]
    else:
        # 必须拷贝一份，防止修改原配置
        sources_to_fetch = list(config.get('enabled_sources', ['115', 'magnet', 'ed2k']))
    
    # 2. 获取过滤配置 (提前获取)
    filters = config.get('filters', {})
    
    # 如果开启了容器过滤，强制跳过磁力链 搜索以节省配额
    allowed_containers = filters.get('containers', [])
    if allowed_containers and 'magnet' in sources_to_fetch:
        logger.debug(f"  ➜ [NULLBR] 检测到开启了容器过滤 ({allowed_containers})，已跳过磁力链搜索以节省配额。")
        sources_to_fetch.remove('magnet')
    
    # 配额检查
    if _user_level_cache.get('daily_quota', 0) > 0 and _user_level_cache.get('daily_used', 0) >= _user_level_cache.get('daily_quota', 0):
        logger.warning(f"  ⚠️ 今日配额已用完，无法请求API搜索资源。")
        raise Exception("今日 API 配额已用完，请明日再试或升级套餐。")

    # ==============================================================================
    # ★★★ 提前计算集数 (用于大小过滤) ★★★
    # ==============================================================================
    episode_count = 0
    should_fetch_ep_count = False
    
    # 只有是剧集且有季号时才考虑
    if media_type == 'tv' and season_number is not None:
        # 检查是否配置了大小限制
        t_min = filters.get('tv_min_size')
        if t_min is None: t_min = filters.get('min_size')
        
        t_max = filters.get('tv_max_size')
        if t_max is None: t_max = filters.get('max_size')
        
        try:
            if (t_min and float(t_min) > 0) or (t_max and float(t_max) > 0):
                should_fetch_ep_count = True
        except:
            pass 

    if should_fetch_ep_count:
        try:
            tmdb_api_key = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_TMDB_API_KEY)
            if tmdb_api_key:
                season_info = tmdb.get_tv_season_details(tmdb_id, season_number, tmdb_api_key)
                if season_info and 'episodes' in season_info:
                    episode_count = len(season_info['episodes'])
                    logger.info(f"  ➜ [NULLBR] 获取到 （第 {season_number} 季） 总集数: {episode_count}，将按单集平均大小过滤。")
        except Exception as e:
            logger.warning(f"  ⚠️ 获取 TMDb 季集数失败: {e}")

    # ==============================================================================
    # ★★★ 循环获取并分别过滤 ★★★
    # ==============================================================================
    final_filtered_list = []
    
    # 定义源名称映射
    source_name_map = {
        '115': '115分享',
        'magnet': '磁力链',
        'ed2k': '电驴(Ed2k)'
    }

    for source in sources_to_fetch:
        try:
            # 针对 ed2k 的特殊判断 (TV 不搜 ed2k)
            if media_type == 'tv' and source == 'ed2k':
                if episode_number is None:
                    continue
                
            # 1. 获取原始资源
            raw_res = _fetch_single_source(tmdb_id, media_type, source, season_number, episode_number)
            
            if not raw_res:
                continue

            # 2. 立即执行过滤
            current_filtered = [
                res for res in raw_res 
                if _is_resource_valid(res, filters, media_type, episode_count=episode_count)
            ]
            
            # 3. 打印带源名称的日志
            cn_name = source_name_map.get(source, source.upper())
            logger.info(f"  ➜ {cn_name} 资源过滤: 原始 {len(raw_res)} -> 过滤后 {len(current_filtered)}")
            
            # 4. 加入最终列表
            if current_filtered:
                final_filtered_list.extend(current_filtered)

        except Exception as e:
            logger.warning(f"  ➜ 获取 {source} 资源异常: {e}")

    return final_filtered_list

# ==============================================================================
# ★★★ 智能整理核心逻辑 (Smart Organizer) ★★★
# ==============================================================================

class SmartOrganizer:
    def __init__(self, client, tmdb_id, media_type, original_title):
        self.client = client
        self.tmdb_id = tmdb_id
        self.media_type = media_type
        self.original_title = original_title
        self.api_key = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_TMDB_API_KEY)
        
        # 加载映射表 (用于工作室/关键词/分级的逻辑匹配)
        self.studio_map = settings_db.get_setting('studio_mapping') or utils.DEFAULT_STUDIO_MAPPING
        self.keyword_map = settings_db.get_setting('keyword_mapping') or utils.DEFAULT_KEYWORD_MAPPING
        self.rating_map = settings_db.get_setting('rating_mapping') or utils.DEFAULT_RATING_MAPPING
        self.rating_priority = settings_db.get_setting('rating_priority') or utils.DEFAULT_RATING_PRIORITY
        
        # 提取原始数据
        self.raw_metadata = self._fetch_raw_metadata()
        self.details = self.raw_metadata
        self.rules = settings_db.get_setting('nullbr_sorting_rules') or []

    def _fetch_raw_metadata(self):
        """
        获取 TMDb 原始元数据 (ID/Code)，不进行任何中文转换。
        """
        if not self.api_key: return {}
        
        data = {
            'genre_ids': [], 
            'country_codes': [], 
            'lang_code': None, 
            'company_ids': [], 
            'network_ids': [],
            'keyword_ids': [], 
            'rating_label': '未知' # 分级是特例，必须计算出标签才能匹配
        }

        try:
            raw_details = {}
            if self.media_type == 'tv':
                raw_details = tmdb.get_tv_details(
                    self.tmdb_id, self.api_key, 
                    append_to_response="keywords,content_ratings,networks"
                )
            else:
                raw_details = tmdb.get_movie_details(
                    self.tmdb_id, self.api_key, 
                    append_to_response="keywords,release_dates"
                )

            if not raw_details: return {}

            # 1. 基础 ID/Code 提取
            data['genre_ids'] = [g.get('id') for g in raw_details.get('genres', [])]
            data['country_codes'] = [c.get('iso_3166_1') for c in raw_details.get('production_countries', [])]
            if not data['country_codes'] and raw_details.get('origin_country'):
                data['country_codes'] = raw_details.get('origin_country')
            
            data['lang_code'] = raw_details.get('original_language')
            
            data['company_ids'] = [c.get('id') for c in raw_details.get('production_companies', [])]
            data['network_ids'] = [n.get('id') for n in raw_details.get('networks', [])] if self.media_type == 'tv' else []

            # 2. 关键词 ID 提取
            kw_container = raw_details.get('keywords', {})
            raw_kw_list = kw_container.get('keywords', []) if self.media_type == 'movie' else kw_container.get('results', [])
            data['keyword_ids'] = [k.get('id') for k in raw_kw_list]

            # 3. 分级计算 (这是唯一需要预处理成 Label 的，因为它是抽象概念)
            # ... (保留原有的分级计算逻辑，计算出 rating_label) ...
            rating_code = None
            rating_country = None
            if self.media_type == 'tv':
                results = raw_details.get('content_ratings', {}).get('results', [])
                for country in self.rating_priority:
                    if country == 'ORIGIN': continue 
                    found = next((r['rating'] for r in results if r['iso_3166_1'] == country), None)
                    if found:
                        rating_code = found
                        rating_country = country
                        break
            else:
                results = raw_details.get('release_dates', {}).get('results', [])
                for country in self.rating_priority:
                    if country == 'ORIGIN': continue
                    country_release = next((r for r in results if r['iso_3166_1'] == country), None)
                    if country_release:
                        cert = next((x['certification'] for x in country_release.get('release_dates', []) if x.get('certification')), None)
                        if cert:
                            rating_code = cert
                            rating_country = country
                            break
            
            if rating_code and rating_country:
                country_map_list = self.rating_map.get(rating_country, [])
                label_match = next((r['label'] for r in country_map_list if r['code'] == rating_code), None)
                if label_match:
                    data['rating_label'] = label_match

            # 补充标题日期供重命名
            data['title'] = raw_details.get('title') or raw_details.get('name')
            data['date'] = raw_details.get('release_date') or raw_details.get('first_air_date')

            return data

        except Exception as e:
            logger.warning(f"  ⚠️ [整理] 获取原始元数据失败: {e}", exc_info=True)
            return {}

    def _match_rule(self, rule):
        """
        规则匹配逻辑：
        - 标准字段：直接比对 ID/Code
        - 集合字段（工作室/关键词）：通过 Label 反查 Config 中的 ID 列表，再比对 TMDb ID
        """
        if not self.raw_metadata: return False
        
        # 1. 媒体类型
        if rule.get('media_type') and rule['media_type'] != 'all':
            if rule['media_type'] != self.media_type: return False

        # 2. 类型 (Genres) - ID 匹配
        if rule.get('genres'):
            # rule['genres'] 存的是 ID 列表 (如 [16, 35])
            # self.raw_metadata['genre_ids'] 是 TMDb ID 列表
            # 只要有一个交集就算命中
            rule_ids = [int(x) for x in rule['genres']]
            if not any(gid in self.raw_metadata['genre_ids'] for gid in rule_ids): return False

        # 3. 国家 (Countries) - Code 匹配
        if rule.get('countries'):
            # rule['countries'] 存的是 Code (如 ['US', 'CN'])
            if not any(c in self.raw_metadata['country_codes'] for c in rule['countries']): return False

        # 4. 语言 (Languages) - Code 匹配
        if rule.get('languages'):
            if self.raw_metadata['lang_code'] not in rule['languages']: return False

        # 5. 工作室 (Studios) - Label -> ID 匹配
        if rule.get('studios'):
            # rule['studios'] 存的是 Label (如 ['漫威', 'Netflix'])
            # 我们需要遍历这些 Label，去 self.studio_map 里找对应的 ID
            target_ids = set()
            for label in rule['studios']:
                # 找到配置项
                config_item = next((item for item in self.studio_map if item['label'] == label), None)
                if config_item:
                    target_ids.update(config_item.get('company_ids', []))
                    target_ids.update(config_item.get('network_ids', []))
            
            # 检查 TMDb 的 company/network ID 是否在 target_ids 中
            has_company = any(cid in target_ids for cid in self.raw_metadata['company_ids'])
            has_network = any(nid in target_ids for nid in self.raw_metadata['network_ids'])
            
            if not (has_company or has_network): return False
            
        # 6. 关键词 (Keywords) - Label -> ID 匹配
        if rule.get('keywords'):
            target_ids = set()
            for label in rule['keywords']:
                config_item = next((item for item in self.keyword_map if item['label'] == label), None)
                if config_item:
                    target_ids.update(config_item.get('ids', []))
            
            # 兼容字符串/数字 ID
            tmdb_kw_ids = [int(k) for k in self.raw_metadata['keyword_ids']]
            target_ids_int = [int(k) for k in target_ids]
            
            if not any(kid in target_ids_int for kid in tmdb_kw_ids): return False

        # 7. 分级 (Rating) - Label 匹配
        if rule.get('ratings'):
            if self.raw_metadata['rating_label'] not in rule['ratings']: return False

        return True

    def get_target_cid(self):
        """遍历规则，返回命中的 CID。未命中返回 None"""
        for rule in self.rules:
            if not rule.get('enabled', True): continue
            if self._match_rule(rule):
                logger.info(f"  🎯 [整理] 命中规则: {rule.get('name')} -> CID: {rule.get('cid')}")
                return rule.get('cid')
        return None

    def _extract_video_info(self, filename):
        """
        从文件名提取视频信息 (来源 · 分辨率 · 编码 · 音频 · 制作组)
        参考格式: BluRay · 1080p · X264 · DDP 7.1 · CMCT
        """
        info_tags = []
        name_upper = filename.upper()
        
        # 1. 来源/质量 (Source)
        source = ""
        if re.search(r'REMUX', name_upper): source = 'Remux'
        elif re.search(r'BLU-?RAY|BD', name_upper): source = 'BluRay'
        elif re.search(r'WEB-?DL', name_upper): source = 'WEB-DL'
        elif re.search(r'WEB-?RIP', name_upper): source = 'WEBRip'
        elif re.search(r'HDTV', name_upper): source = 'HDTV'
        elif re.search(r'DVD', name_upper): source = 'DVD'
        
        # 2. 特效 (Effect: HDR/DV)
        effect = ""
        is_dv = re.search(r'\b(DV|DOVI|DOLBY\s?VISION)\b', name_upper)
        is_hdr = re.search(r'\b(HDR|HDR10\+?)\b', name_upper)
        
        if is_dv and is_hdr: effect = "HDR" # 通常文件名写 WEB-DL HDR DV，这里简化显示，或者组合
        elif is_dv: effect = "DV"
        elif is_hdr: effect = "HDR"
        
        # 组合 Source 和 Effect (如 WEB-DL HDR)
        if source:
            info_tags.append(f"{source} {effect}".strip())
        elif effect:
            info_tags.append(effect)

        # 3. 分辨率 (Resolution)
        res_match = re.search(r'(2160|1080|720|480)[pP]', filename)
        if res_match:
            info_tags.append(res_match.group(0).lower())
        elif '4K' in name_upper:
            info_tags.append('2160p')

        # 4. 编码 (Codec)
        if re.search(r'[HX]265|HEVC', name_upper): info_tags.append('H265')
        elif re.search(r'[HX]264|AVC', name_upper): info_tags.append('H264')
        elif re.search(r'AV1', name_upper): info_tags.append('AV1')
        elif re.search(r'MPEG-?2', name_upper): info_tags.append('MPEG2')

        # 5. 音频 (Audio)
        audio_info = []
        # 音频编码
        if re.search(r'ATMOS', name_upper): audio_info.append('Atmos')
        elif re.search(r'TRUEHD', name_upper): audio_info.append('TrueHD')
        elif re.search(r'DTS-?HD(\s?MA)?', name_upper): audio_info.append('DTS-HD')
        elif re.search(r'DTS', name_upper): audio_info.append('DTS')
        elif re.search(r'DDP|EAC3|DOLBY\s?DIGITAL\+', name_upper): audio_info.append('DDP')
        elif re.search(r'AC3|DD', name_upper): audio_info.append('AC3')
        elif re.search(r'AAC', name_upper): audio_info.append('AAC')
        elif re.search(r'FLAC', name_upper): audio_info.append('FLAC')
        
        # 声道
        chan_match = re.search(r'\b(7\.1|5\.1|2\.0)\b', filename)
        if chan_match:
            audio_info.append(chan_match.group(1))
            
        if audio_info:
            info_tags.append(" ".join(audio_info))

        # 6. 发布组 (Release Group) - 调用 helpers.RELEASE_GROUPS
        # 逻辑：遍历所有正则，如果匹配到，提取文件名中的原始字符串
        group_found = False
        for group_key, patterns in utils.RELEASE_GROUPS.items() if hasattr(utils, 'RELEASE_GROUPS') else {}.items():
             # 注意：这里假设 helpers 被 import 为 utils 或者 helpers，根据文件头 import 情况调整
             # 原文件 import utils, 但 RELEASE_GROUPS 在 helpers.py。
             # 如果 nullbr.py 没有 import helpers，需要确保能访问到。
             # 假设 helpers.py 的内容在 helpers 模块中，或者被 utils 引用。
             # 既然你提供了 helpers.py，且 nullbr.py 头部没有 import helpers，
             # **请确保在 nullbr.py 头部添加: import handler.helpers as helpers 或 from tasks import helpers**
             pass

        # 修正：直接使用 helpers 模块 (需要在文件头 import tasks.helpers as helpers)
        # 考虑到原文件结构，这里尝试从 helpers 匹配
        try:
            from tasks import helpers # 延迟导入防止循环引用，或者放在文件头
            for group_name, patterns in helpers.RELEASE_GROUPS.items():
                for pattern in patterns:
                    try:
                        # 使用正则查找文件名中的组名
                        match = re.search(pattern, filename, re.IGNORECASE)
                        if match:
                            # 匹配到了，保留文件名中的原始写法 (match.group(0))
                            info_tags.append(match.group(0))
                            group_found = True
                            break
                    except: pass
                if group_found: break
            
            # 如果没在字典里找到，尝试匹配常见的 -Group 结尾
            if not group_found:
                # 匹配文件名末尾的 -Group (如 -CMCT.mkv)
                # 去掉扩展名
                name_no_ext = os.path.splitext(filename)[0]
                match_suffix = re.search(r'-([a-zA-Z0-9]+)$', name_no_ext)
                if match_suffix:
                    possible_group = match_suffix.group(1)
                    # 排除常见非组名后缀
                    if len(possible_group) > 2 and possible_group.upper() not in ['1080P', '2160P', '4K', 'HDR', 'H265', 'H264']:
                        info_tags.append(possible_group)
        except ImportError:
            pass

        return " · ".join(info_tags) if info_tags else ""

    def _rename_file_node(self, file_node, new_base_name, is_tv=False):
        """重命名单个文件节点"""
        original_name = file_node.get('n', '')
        ext = original_name.split('.')[-1]
        
        # 提取标签信息
        video_info = self._extract_video_info(original_name)
        
        # 构造后缀：注意这里使用 " · " 作为分隔符
        suffix = f" · {video_info}" if video_info else ""
        
        if is_tv:
            # 剧集：尝试提取 SxxExx
            # 匹配 S01E01, S1E1, Ep01, 第01集
            pattern = r'(?:s|S)(\d{1,2})(?:e|E)(\d{1,2})|Ep?(\d{1,2})|第(\d{1,3})[集话]'
            match = re.search(pattern, original_name)
            if match:
                s, e, ep_only, zh_ep = match.groups()
                season_num = int(s) if s else 1
                episode_num = int(e) if e else (int(ep_only) if ep_only else int(zh_ep))
                
                # 格式化为 S01E01
                s_str = f"S{season_num:02d}"
                e_str = f"E{episode_num:02d}"
                
                # 剧集格式：Title - S01E01 · Tags.ext
                new_name = f"{new_base_name} - {s_str}{e_str}{suffix}.{ext}"
                
                return new_name, season_num
            else:
                # 没匹配到集数，不改名
                return original_name, None
        else:
            # 电影格式：Title (Year) · Tags.ext
            new_name = f"{new_base_name}{suffix}.{ext}"
            return new_name, None

    def execute(self, root_item, target_cid):
        """执行整理：区分单文件归档与文件夹整理"""
        # 1. 准备标准名称 (作为文件夹名)
        title = self.details.get('title') or self.original_title
        date_str = self.details.get('date') or ''
        year = date_str[:4] if date_str else ''
        
        # 替换非法字符
        safe_title = re.sub(r'[\\/:*?"<>|]', '', title).strip()
        std_root_name = f"{safe_title} ({year}) {{tmdb-{self.tmdb_id}}}" if year else f"{safe_title} {{tmdb-{self.tmdb_id}}}"
        
        # 2. 识别类型
        root_id = root_item.get('fid') or root_item.get('cid')
        # 115 API: 有 fid 的是文件，没有 fid (只有 cid) 的是文件夹
        is_file = bool(root_item.get('fid'))
        
        # ==================================================
        # 分支 A: 单文件处理 (创建文件夹 -> 移动 -> 改名)
        # ==================================================
        if is_file:
            logger.info(f"  🛠️ [整理] 识别为单文件，执行归档模式...")
            
            # A1. 确定新文件夹创建在哪里
            # 如果有目标 target_cid (命中规则)，就去那里建
            # 如果没有 (未命中规则)，就在当前文件所在的目录建 (root_item['cid'] 即为父目录id)
            dest_parent_cid = target_cid if (target_cid and str(target_cid) != '0') else root_item.get('cid')
            
            # A2. 创建标准命名的文件夹
            mk_res = self.client.fs_mkdir(std_root_name, dest_parent_cid)
            new_folder_cid = mk_res.get('cid')
            
            if not new_folder_cid:
                logger.error(f"  ❌ [整理] 创建文件夹失败: {std_root_name}")
                return False
                
            # A3. 将文件移动到新文件夹内
            self.client.fs_move(root_id, new_folder_cid)
            
            # A4. 重命名文件本身 (加上后缀和Tags)
            new_filename, _ = self._rename_file_node(root_item, safe_title, is_tv=(self.media_type=='tv'))
            
            if new_filename != root_item.get('n'):
                self.client.fs_rename((root_id, new_filename))
                logger.info(f"  ✅ [整理] 单文件归档完成: {new_filename}")
            else:
                logger.info(f"  ✅ [整理] 单文件归档完成 (无需改名)")

        # ==================================================
        # 分支 B: 文件夹处理 (重命名文件夹 -> 内部整理 -> 移动)
        # ==================================================
        else:
            logger.info(f"  🛠️ [整理] 识别为文件夹，执行重命名: {root_item.get('n')} -> {std_root_name}")
            
            # B1. 重命名根文件夹
            self.client.fs_rename((root_id, std_root_name))
            
            # B2. 进入内部处理 (重命名视频文件 + 剧集归类 + 垃圾清理)
            files_res = self.client.fs_files({'cid': root_id, 'limit': 1000})
            if files_res.get('data'):
                season_folders_cache = {} # { season_num: folder_cid }
                
                # 定义白名单后缀 (视频 + 字幕)
                video_exts = ['mp4', 'mkv', 'avi', 'ts', 'iso', 'rmvb', 'wmv', 'mov', 'm2ts']
                sub_exts = ['srt', 'ass', 'ssa', 'sub', 'vtt', 'sup']
                
                for sub_file in files_res['data']:
                    fid = sub_file.get('fid')
                    if not fid: continue # 忽略子文件夹
                    
                    file_name = sub_file.get('n', '')
                    ext = file_name.split('.')[-1].lower() if '.' in file_name else ''
                    
                    # --- 垃圾清理 ---
                    is_video = ext in video_exts
                    is_sub = ext in sub_exts
                    
                    if not (is_video or is_sub):
                        logger.info(f"  🗑️ [整理] 删除垃圾文件: {file_name}")
                        self.client.fs_delete([fid])
                        continue
                        
                    # 视频大小检查 (<100MB 删除)
                    if is_video:
                        should_delete = False
                        raw_size = sub_file.get('size')
                        try:
                            if isinstance(raw_size, (int, float)):
                                if raw_size < 100 * 1024 * 1024: should_delete = True
                            elif isinstance(raw_size, str):
                                s_upper = raw_size.upper().replace(',', '')
                                if 'GB' not in s_upper and 'TB' not in s_upper:
                                    if 'KB' in s_upper or 'BYTES' in s_upper: should_delete = True
                                    elif 'MB' in s_upper:
                                        match = re.search(r'([\d\.]+)', s_upper)
                                        if match and float(match.group(1)) < 100: should_delete = True
                        except: pass

                        if should_delete:
                            logger.info(f"  🗑️ [整理] 删除过小视频: {file_name}")
                            self.client.fs_delete([fid])
                            continue
                    
                    # --- 视频文件重命名 ---
                    if is_video:
                        new_filename, season_num = self._rename_file_node(sub_file, safe_title, is_tv=(self.media_type=='tv'))
                        
                        if new_filename != file_name:
                            self.client.fs_rename((fid, new_filename))
                        
                        # 剧集：移动到 Season 目录
                        if self.media_type == 'tv' and season_num is not None:
                            s_folder_cid = season_folders_cache.get(season_num)
                            if not s_folder_cid:
                                s_name = f"Season {season_num:02d}"
                                found = False
                                for existing in files_res['data']:
                                    if existing.get('n') == s_name and existing.get('cid'):
                                        s_folder_cid = existing.get('cid')
                                        found = True
                                        break
                                if not found:
                                    mk_res = self.client.fs_mkdir(s_name, root_id)
                                    if mk_res.get('state'): s_folder_cid = mk_res.get('cid')
                                
                                if s_folder_cid: season_folders_cache[season_num] = s_folder_cid
                            
                            if s_folder_cid:
                                self.client.fs_move(fid, s_folder_cid)

            # B3. 整体移动到目标 CID
            if target_cid and str(target_cid) != '0':
                logger.info(f"  🚚 [整理] 移动文件夹到分类目录 CID: {target_cid}")
                self.client.fs_move(root_id, target_cid)
        
        return True

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
    """
    config = get_config()
    cms_url = config.get('cms_url')
    cms_token = config.get('cms_token')

    if not cms_url or not cms_token:
        return

    cms_url = cms_url.rstrip('/')
    
    # ★★★ 核心修改：根据是否启用智能整理，选择不同的接口 ★★★
    enable_smart_organize = config.get('enable_smart_organize', False)
    
    if enable_smart_organize:
        # 智能整理模式：文件已归位，执行增量同步 (lift_sync)
        api_url = f"{cms_url}/api/sync/lift_by_token"
        params = {
            "type": "lift_sync",
            "token": cms_token
        }
        logger.info(f"  ➜ [CMS] 通知 CMS 执行增量同步 ...")
    else:
        # 默认模式：文件在下载目录，执行自动整理 (auto_organize)
        api_url = f"{cms_url}/api/sync/lift_by_token"
        params = {
            "type": "auto_organize",
            "token": cms_token
        }
        logger.info(f"  ➜ [CMS] 通知 CMS 执行自动整理 ...")

    try:
        response = requests.get(api_url, params=params, timeout=5)
        response.raise_for_status()
        
        res_json = response.json()
        if res_json.get('code') == 200 or res_json.get('success'):
            logger.info(f"  ✅ CMS 通知成功: {res_json.get('msg', 'OK')}")
        else:
            logger.warning(f"  ⚠️ CMS 通知返回异常: {res_json}")

    except Exception as e:
        logger.warning(f"  ⚠️ CMS 通知发送失败: {e}")
        # 不抛出异常，以免影响主流程

def _standardize_115_file(client, file_item, save_cid, raw_title, tmdb_id, media_type='movie'):
    """
    修复版：对 115 新入库的文件/文件夹进行标准化重命名
    """
    try:
        # ==================================================
        # 1. 获取官方元数据 (TMDb) - 保持原逻辑
        # ==================================================
        final_title = raw_title
        final_year = None
        
        try:
            tmdb_api_key = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_TMDB_API_KEY)
            if tmdb_api_key and tmdb_id:
                details = None
                if media_type == 'tv':
                    details = tmdb.get_tv_details(tmdb_id, tmdb_api_key)
                    if details:
                        final_title = details.get('name')
                        first_air_date = details.get('first_air_date')
                        if first_air_date: final_year = first_air_date[:4]
                else:
                    details = tmdb.get_movie_details(tmdb_id, tmdb_api_key)
                    if details:
                        final_title = details.get('title')
                        release_date = details.get('release_date')
                        if release_date: final_year = release_date[:4]
        except Exception as e:
            logger.warning(f"  ⚠️ [整理] TMDb 获取失败: {e}")

        if not final_year:
            match = re.search(r'[(（](\d{4})[)）]', raw_title)
            if match: final_year = match.group(1)
        
        safe_title = re.sub(r'[\\/:*?"<>|]', '', final_title).strip()
        std_name = f"{safe_title} ({final_year}) {{tmdb-{tmdb_id}}}" if final_year else f"{safe_title} {{tmdb-{tmdb_id}}}"

        # ==================================================
        # 2. 核心修复：区分 文件夹重命名 与 单文件归档
        # ==================================================
        # 115 文件夹标识：ico == 'folder' 或者没有 fid (只有 cid)
        is_directory = (file_item.get('ico') == 'folder') or (not file_item.get('fid'))
        current_name = file_item.get('n')

        if current_name == std_name:
            logger.info(f"  ✅ [整理] 名称已符合标准，跳过操作。")
            return

        if is_directory:
            folder_id = file_item.get('cid')
            logger.info(f"  🛠️ [整理] 识别为文件夹，执行重命名: {current_name} -> {std_name}")
            
            # 修复：将两个参数封装成一个元组传入
            rename_res = client.fs_rename((folder_id, std_name)) 
            
            if isinstance(rename_res, dict) and rename_res.get('state'):
                logger.info(f"  ✅ [整理] 文件夹重命名成功")
            else:
                logger.warning(f"  ⚠️ [整理] 重命名失败: {rename_res}")
        
        else:
            # === 情况 B: 单文件归档 ===
            file_id = file_item.get('fid')
            logger.info(f"  🛠️ [整理] 识别为单文件，正在归档至目录: {std_name}")
            
            # 检查目标文件夹是否存在
            target_dir_cid = None
            # 这里的 search 逻辑要小心，115 的搜索返回结构可能不同
            search_res = client.fs_files({'cid': save_cid, 'search_value': std_name})
            if isinstance(search_res, dict) and search_res.get('data'):
                for item in search_res['data']:
                    if item.get('n') == std_name and (item.get('ico') == 'folder' or not item.get('fid')):
                        target_dir_cid = item.get('cid')
                        break
            
            if not target_dir_cid:
                mkdir_res = client.fs_mkdir(std_name, save_cid)
                if isinstance(mkdir_res, dict) and mkdir_res.get('state'):
                    target_dir_cid = mkdir_res.get('cid')
                else:
                    logger.error(f"  ❌ [整理] 创建文件夹失败")
                    return 

            # 执行移动
            move_res = client.fs_move([file_id], target_dir_cid)
            if isinstance(move_res, dict) and move_res.get('state'):
                logger.info(f"  ✅ [整理] 单文件已归档成功")
            else:
                logger.warning(f"  ⚠️ [整理] 移动文件失败")

    except Exception as e:
        # 这里会捕获到 "not enough values to unpack" 并打印具体位置
        logger.error(f"  ⚠️ 标准化重命名流程异常: {e}", exc_info=True)

def push_to_115(resource_link, title, tmdb_id=None, media_type=None):
    """
    智能推送：支持 115/115cdn/anxia 转存 和 磁力离线
    并执行 智能整理 (Smart Organize)
    """
    if P115Client is None:
        raise ImportError("未安装 p115 库")

    config = get_config()
    cookies = config.get('p115_cookies')
    
    # 默认保存路径 (中转站)
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
    
    # ==================================================
    # ★★★ 步骤 1: 建立目录快照 (用于捕获新文件) ★★★
    # ==================================================
    existing_ids = set()
    try:
        # 扫描前50个文件即可，通常新文件在最前
        files_res = client.fs_files({'cid': save_path_cid, 'limit': 50, 'o': 'user_ptime', 'asc': 0})
        if files_res.get('data'):
            for item in files_res['data']:
                item_id = item.get('fid') or item.get('cid') 
                if item_id: existing_ids.add(str(item_id))
    except Exception as e:
        logger.warning(f"  ⚠️ 获取目录快照失败: {e}")

    # ==================================================
    # ★★★ 步骤 2: 执行任务 (转存 或 离线) ★★★
    # ==================================================
    # ... (这部分代码保持不变，负责调用 115 API 添加任务) ...
    target_domains = ['115.com', '115cdn.com', 'anxia.com']
    is_115_share = any(d in clean_url for d in target_domains) and ('magnet' not in clean_url)
    task_success = False
    
    try:
        if is_115_share:
            # ... (115 分享转存逻辑，保持不变) ...
            logger.info(f"  ➜ [NULLBR] 识别为 115 转存任务 -> CID: {save_path_cid}")
            share_code = None
            match = re.search(r'/s/([a-z0-9]+)', clean_url)
            if match: share_code = match.group(1)
            if not share_code: raise Exception("无法提取分享码")
            receive_code = ''
            pwd_match = re.search(r'password=([a-z0-9]+)', clean_url)
            if pwd_match: receive_code = pwd_match.group(1)
            
            resp = {} 
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

            if resp and resp.get('state'):
                logger.info(f"  ✅ 115 转存请求成功")
                task_success = True
            else:
                err = resp.get('error_msg') or resp.get('msg') or str(resp)
                raise Exception(f"转存失败: {err}")
        else:
            # ... (磁力离线逻辑，保持不变) ...
            logger.info(f"  ➜ [NULLBR] 识别为磁力/离线任务 -> CID: {save_path_cid}")
            payload = {'url[0]': clean_url, 'wp_path_id': save_path_cid}
            resp = client.offline_add_urls(payload)
            if resp.get('state'):
                task_success = True
                logger.info(f"  ➜ [NULLBR] 任务已提交，等待文件生成...")
            else:
                err = resp.get('error_msg') or resp.get('msg') or '未知错误'
                if '已存在' in str(err):
                    task_success = True
                    logger.info(f"  ✅ 任务已存在")
                else:
                    raise Exception(f"离线失败: {err}")
    except Exception as e:
        raise e

    # ==================================================
    # ★★★ 步骤 3: 扫描新文件并执行智能整理 ★★★
    # ==================================================
    if task_success:
        # 轮询查找新文件
        max_retries = 8 # 稍微增加重试次数
        found_item = None
        
        for i in range(max_retries):
            time.sleep(3)
            try:
                check_res = client.fs_files({'cid': save_path_cid, 'limit': 50, 'o': 'user_ptime', 'asc': 0})
                if check_res.get('data'):
                    for item in check_res['data']:
                        current_id = item.get('fid') or item.get('cid')
                        if current_id and (str(current_id) not in existing_ids):
                            found_item = item
                            break
                if found_item:
                    break
            except Exception as e:
                logger.debug(f"轮询出错: {e}")
        
        if found_item:
            item_name = found_item.get('n', '未知')
            logger.info(f"  ✅ 捕获到新入库项目: {item_name}")
            
            # ★★★ 核心修改：调用智能整理 ★★★
            if tmdb_id:
                try:
                    # 检查是否开启了整理功能
                    enable_organize = config.get('enable_smart_organize', False)
                    
                    if enable_organize:
                        logger.info("  🧠 [整理] 智能整理已开启，开始分析...")
                        organizer = SmartOrganizer(client, tmdb_id, media_type, title)
                        target_cid = organizer.get_target_cid()
                        
                        # 无论是否命中规则，只要开启了整理，就执行重命名
                        # 如果没命中规则，target_cid 为 None，则只重命名不移动
                        organizer.execute(found_item, target_cid)
                    else:
                        # 旧逻辑：仅简单重命名
                        _standardize_115_file(client, found_item, save_path_cid, title, tmdb_id, media_type)
                        
                except Exception as e:
                    logger.error(f"  ❌ [整理] 智能整理执行失败: {e}", exc_info=True)
            else:
                logger.debug("  ⚠️ 未提供 TMDb ID，跳过整理")
            
            return True
        else:
            if is_115_share:
                logger.warning("  ⚠️ 转存显示成功但未捕获到新文件ID (可能文件已存在)")
                return True
            else:
                logger.warning("  ❌ 离线任务超时，未在目录发现新文件 (死链或下载过慢)")
                # 磁力链可能需要很久，这里不报错，只是无法执行整理
                return True

    return False

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

def handle_push_request(link, title, tmdb_id=None, media_type=None):
    """
    统一推送入口
    """
    # 1. 推送到 115 (传递 ID 以便重命名)
    push_to_115(link, title, tmdb_id, media_type)
    
    # 2. 115 成功后，通知 CMS 整理
    notify_cms_scan()
    
    return True

def auto_download_best_resource(tmdb_id, media_type, title, season_number=None, episode_number=None):
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

            resources = fetch_resource_list(tmdb_id, media_type, specific_source=source, season_number=season_number, episode_number=episode_number)
            
            if not resources:
                continue

            logger.info(f"  ➜ [{source.upper()}] 找到 {len(resources)} 个资源，开始尝试推送...")

            for index, res in enumerate(resources):
                try:
                    logger.info(f"  👉 尝试第 {index + 1} 个资源: {res['title']}")
                    
                    # 调用统一推送入口 (115 -> CMS Notify)
                    handle_push_request(res['link'], title, tmdb_id, media_type)
                    
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
        logger.error(f"  ➜ NULLBR 搜索失败: {e}")
        return False