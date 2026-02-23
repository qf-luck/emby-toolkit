# handler/p115_service.py
import logging
import requests
import random
import os
import json
import re
import threading
import time
import config_manager
import constants
from database import settings_db
from database.connection import get_db_connection
import handler.tmdb as tmdb
import utils
try:
    from p115client import P115Client
except ImportError:
    P115Client = None

logger = logging.getLogger(__name__)

# ======================================================================
# ★★★ 新增：115 目录树 DB 缓存管理器 ★★★
# ======================================================================
class P115CacheManager:
    @staticmethod
    def get_cid(parent_cid, name):
        """从本地数据库获取 CID (毫秒级)"""
        if not parent_cid or not name: return None
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM p115_filesystem_cache WHERE parent_id = %s AND name = %s", 
                        (str(parent_cid), str(name))
                    )
                    row = cursor.fetchone()
                    return row['id'] if row else None
        except Exception as e:
            logger.error(f"  ❌ 读取 115 DB 缓存失败: {e}")
            return None

    @staticmethod
    def save_cid(cid, parent_cid, name):
        """将 CID 存入本地数据库缓存"""
        if not cid or not parent_cid or not name: return
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO p115_filesystem_cache (id, parent_id, name)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (parent_id, name)
                        DO UPDATE SET id = EXCLUDED.id, updated_at = NOW()
                    """, (str(cid), str(parent_cid), str(name)))
                    conn.commit()
        except Exception as e:
            logger.error(f"  ❌ 写入 115 DB 缓存失败: {e}")

    @staticmethod
    def get_cid_by_name(name):
        """仅通过名称查找 CID (适用于带有 {tmdb=xxx} 的唯一主目录)"""
        if not name: return None
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT id FROM p115_filesystem_cache WHERE name = %s LIMIT 1", (str(name),))
                    row = cursor.fetchone()
                    return row['id'] if row else None
        except Exception as e:
            return None

    @staticmethod
    def delete_cid(cid):
        """从缓存中物理删除该目录及其子目录的记录"""
        if not cid: return
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    # 删除自身以及以它为父目录的子项
                    cursor.execute("DELETE FROM p115_filesystem_cache WHERE id = %s OR parent_id = %s", (str(cid), str(cid)))
                    conn.commit()
        except Exception as e:
            logger.error(f"  ❌ 清理 115 DB 缓存失败: {e}")

def get_config():
    return config_manager.APP_CONFIG

class P115Service:
    _instance = None
    _lock = threading.Lock()
    _client = None
    _last_request_time = 0
    _cookies_cache = None

    @classmethod
    def get_client(cls):
        """获取全局唯一的 P115Client 实例 (带自动重载和限流)"""
        if P115Client is None:
            raise ImportError("未安装 p115client")

        # 获取配置
        config = get_config()
        cookies = config.get(constants.CONFIG_OPTION_115_COOKIES)
        
        if not cookies:
            return None

        with cls._lock:
            # 如果 Cookies 变了，或者客户端还没初始化，就重新初始化
            if cls._client is None or cookies != cls._cookies_cache:
                try:
                    cls._client = P115Client(cookies)
                    cls._cookies_cache = cookies
                    logger.debug("  ✅ P115Client 实例已(重新)初始化")
                except Exception as e:
                    logger.error(f"  ❌ P115Client 初始化失败: {e}")
                    return None
            
            # ★★★ 全局限流逻辑 ★★★
            try:
                interval = float(config.get(constants.CONFIG_OPTION_115_INTERVAL, 5.0))
            except (ValueError, TypeError):
                interval = 5.0
            current_time = time.time()
            elapsed = current_time - cls._last_request_time
            
            if elapsed < interval:
                sleep_time = interval - elapsed
                # 只有等待时间超过1秒才打印日志，避免刷屏
                if sleep_time > 1:
                    logger.debug(f"  ⏳ [115限流] 全局等待 {sleep_time:.2f} 秒...")
                time.sleep(sleep_time)
            
            cls._last_request_time = time.time()
            
            return cls._client

    @classmethod
    def get_cookies(cls):
        config = get_config()
        return config.get(constants.CONFIG_OPTION_115_COOKIES)
    
class SmartOrganizer:
    def __init__(self, client, tmdb_id, media_type, original_title):
        self.client = client
        self.tmdb_id = tmdb_id
        self.media_type = media_type
        self.original_title = original_title
        self.api_key = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_TMDB_API_KEY)

        self.studio_map = settings_db.get_setting('studio_mapping') or utils.DEFAULT_STUDIO_MAPPING
        self.keyword_map = settings_db.get_setting('keyword_mapping') or utils.DEFAULT_KEYWORD_MAPPING
        self.rating_map = settings_db.get_setting('rating_mapping') or utils.DEFAULT_RATING_MAPPING
        self.rating_priority = settings_db.get_setting('rating_priority') or utils.DEFAULT_RATING_PRIORITY

        self.raw_metadata = self._fetch_raw_metadata()
        self.details = self.raw_metadata
        raw_rules = settings_db.get_setting(constants.DB_KEY_115_SORTING_RULES)
        self.rules = []
        
        if raw_rules:
            if isinstance(raw_rules, list):
                self.rules = raw_rules
            elif isinstance(raw_rules, str):
                try:
                    self.rules = json.loads(raw_rules)
                except Exception as e:
                    logger.error(f"  ❌ 解析 115 分类规则失败: {e}")
                    self.rules = []

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

            # 3. 分级计算 
            data['rating_label'] = utils.get_rating_label(
                raw_details,
                self.media_type,
                self.rating_map,
                self.rating_priority
            )

            # 补充标题日期供重命名
            data['title'] = raw_details.get('title') or raw_details.get('name')
            date_str = raw_details.get('release_date') or raw_details.get('first_air_date')
            data['date'] = date_str
            data['year'] = 0
            
            if date_str and len(str(date_str)) >= 4:
                try:
                    data['year'] = int(str(date_str)[:4])
                except: 
                    pass
            # 补充评分供规则匹配
            data['vote_average'] = raw_details.get('vote_average', 0)

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
            # 只匹配第一个主要国家，避免合拍片误判 
            current_countries = self.raw_metadata.get('country_codes', [])
            # 获取列表中的第一个国家作为主要国家
            primary_country = current_countries[0] if current_countries else None
            
            # 如果没有国家信息，或者主要国家不在规则允许的列表中，则不匹配
            if not primary_country or primary_country not in rule['countries']:
                return False

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

        # 8. 年份 (Year) 
        year_min = rule.get('year_min')
        year_max = rule.get('year_max')
        
        if year_min or year_max:
            current_year = self.raw_metadata.get('year', 0)
            
            # 如果获取不到年份，且设置了年份限制，则视为不匹配
            if current_year == 0: return False
            
            if year_min and current_year < int(year_min): return False
            if year_max and current_year > int(year_max): return False

        # 9. 时长 (Runtime) 
        # 逻辑：电影取 runtime，剧集取 episode_run_time (列表取平均或第一个)
        run_min = rule.get('runtime_min')
        run_max = rule.get('runtime_max')

        if run_min or run_max:
            current_runtime = 0
            if self.media_type == 'movie':
                current_runtime = self.details.get('runtime') or 0
            else:
                # 剧集时长通常是一个列表 [45, 60]，取第一个作为参考
                runtimes = self.details.get('episode_run_time', [])
                if runtimes and len(runtimes) > 0:
                    current_runtime = runtimes[0]

            # 如果获取不到时长，且设置了限制，视为不匹配
            if current_runtime == 0: return False

            if run_min and current_runtime < int(run_min): return False
            if run_max and current_runtime > int(run_max): return False

        # 10. 评分 (Min Rating) - 数值比较
        if rule.get('min_rating') and float(rule['min_rating']) > 0:
            vote_avg = self.details.get('vote_average', 0)
            if vote_avg < float(rule['min_rating']):
                return False

        return True

    def get_target_cid(self):
        """遍历规则，返回命中的 CID。未命中返回 None"""
        for rule in self.rules:
            if not rule.get('enabled', True): continue
            if self._match_rule(rule):
                logger.info(f"  🎯 [115] 命中规则: {rule.get('name')} -> 目录: {rule.get('dir_name')}")
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

        # ★★★ 修复：UHD 识别 ★★★
        if 'UHD' in name_upper:
            if source == 'BluRay': source = 'UHD BluRay'
            elif not source: source = 'UHD'

        # 2. 特效 (Effect: HDR/DV)
        effect = ""
        is_dv = re.search(r'(?:^|[\.\s\-\_])(DV|DOVI|DOLBY\s?VISION)(?:$|[\.\s\-\_])', name_upper)
        is_hdr = re.search(r'(?:^|[\.\s\-\_])(HDR|HDR10\+?)(?:$|[\.\s\-\_])', name_upper)

        if is_dv and is_hdr: effect = "HDR DV"
        elif is_dv: effect = "DV"
        elif is_hdr: effect = "HDR"

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
        codec = ""
        if re.search(r'[HX]265|HEVC', name_upper): info_tags.append('x265')
        elif re.search(r'[HX]264|AVC', name_upper): info_tags.append('H264')
        elif re.search(r'AV1', name_upper): info_tags.append('AV1')
        elif re.search(r'MPEG-?2', name_upper): info_tags.append('MPEG2')
        # 比特率提取 (Bit Depth) 
        bit_depth = ""
        bit_match = re.search(r'(\d{1,2})BIT', name_upper)
        if bit_match:
            bit_depth = f"{bit_match.group(1)}bit" # 统一格式为小写 bit

        # 将编码和比特率组合，比如 "H265 10bit" 或单独 "H265"
        if codec:
            full_codec = f"{codec} {bit_depth}".strip()
            info_tags.append(full_codec)
        elif bit_depth:
            info_tags.append(bit_depth)

        # 5. 音频 (Audio) - ★★★ 修复重点 ★★★
        audio_info = []
        
        # (1) 优先匹配带数字的音轨 (2Audio, 3Audios) 并统一格式为 "xAudios"
        # 正则说明: 匹配边界 + 数字 + 空格(可选) + Audio + s(可选) + 边界
        num_audio_match = re.search(r'\b(\d+)\s?Audios?\b', name_upper, re.IGNORECASE)
        if num_audio_match:
            # 统一格式化为: 数字 + Audios (例如: 2Audios)
            audio_info.append(f"{num_audio_match.group(1)}Audios")
        else:
            # (2) 如果没有数字音轨，再匹配 Multi/Dual 等通用标签
            if re.search(r'\b(Multi|双语|多音轨|Dual-Audio)\b', name_upper, re.IGNORECASE):
                audio_info.append('Multi')

        # (3) 其他具体音频编码
        if re.search(r'ATMOS', name_upper): audio_info.append('Atmos')
        elif re.search(r'TRUEHD', name_upper): audio_info.append('TrueHD')
        elif re.search(r'DTS-?HD(\s?MA)?', name_upper): audio_info.append('DTS-HD')
        elif re.search(r'DTS', name_upper): audio_info.append('DTS')
        elif re.search(r'DDP|EAC3|DOLBY\s?DIGITAL\+', name_upper): audio_info.append('DDP')
        elif re.search(r'AC3|DD', name_upper): audio_info.append('AC3')
        elif re.search(r'AAC', name_upper): audio_info.append('AAC')
        elif re.search(r'FLAC', name_upper): audio_info.append('FLAC')
        elif re.search(r'OPUS', name_upper): audio_info.append('Opus')
        
        chan_match = re.search(r'\b(7\.1|5\.1|2\.0)\b', filename)
        if chan_match:
            audio_info.append(chan_match.group(1))
            
        if audio_info:
            info_tags.append(" ".join(audio_info))

        # 流媒体平台识别
        # 匹配 NF, AMZN, DSNP, HMAX, HULU, NETFLIX, DISNEY+, APPLETV+
        stream_match = re.search(r'\b(NF|AMZN|DSNP|HMAX|HULU|NETFLIX|DISNEY\+|APPLETV\+|B-GLOBAL)\b', name_upper)
        if stream_match:
            info_tags.append(stream_match.group(1))

        # 6. 发布组 (Release Group)
        group_found = False
        try:
            from tasks import helpers
            for group_name, patterns in helpers.RELEASE_GROUPS.items():
                for pattern in patterns:
                    try:
                        match = re.search(pattern, filename, re.IGNORECASE)
                        if match:
                            info_tags.append(match.group(0))
                            group_found = True
                            break
                    except: pass
                if group_found: break

            if not group_found:
                name_no_ext = os.path.splitext(filename)[0]
                match_suffix = re.search(r'-([a-zA-Z0-9]+)$', name_no_ext)
                if match_suffix:
                    possible_group = match_suffix.group(1)
                    if len(possible_group) > 2 and possible_group.upper() not in ['1080P', '2160P', '4K', 'HDR', 'H265', 'H264']:
                        info_tags.append(possible_group)
        except ImportError:
            pass

        return " · ".join(info_tags) if info_tags else ""

    def _rename_file_node(self, file_node, new_base_name, year=None, is_tv=False):
        """
        重命名单个文件节点
        修复：字幕文件先剥离语言标签，再提取Tags，确保能识别到被语言标签挡住的发布组。
        """
        original_name = file_node.get('n', '')
        if '.' not in original_name: return original_name, None

        # 分离文件名和扩展名
        parts = original_name.rsplit('.', 1)
        name_body = parts[0]
        ext = parts[1].lower()

        is_sub = ext in ['srt', 'ass', 'ssa', 'sub', 'vtt', 'sup']

        # -------------------------------------------------
        # 1. 优先计算字幕语言后缀 (为了后续剥离它)
        # -------------------------------------------------
        lang_suffix = ""
        if is_sub:
            # 常见语言代码白名单
            lang_keywords = [
                'zh', 'cn', 'tw', 'hk', 'en', 'jp', 'kr',
                'chs', 'cht', 'eng', 'jpn', 'kor', 'fre', 'spa',
                'default', 'forced', 'tc', 'sc'
            ]

            # 策略A: 检查文件名最后一段 (Movie.chs.srt)
            sub_parts = name_body.split('.')
            if len(sub_parts) > 1:
                last_part = sub_parts[-1].lower()
                if last_part in lang_keywords or '-' in last_part:
                    lang_suffix = f".{sub_parts[-1]}" # 保持原大小写

            # 策略B: 正则搜索
            if not lang_suffix:
                match = re.search(r'(?:\.|-|_|\s)(chs|cht|zh-cn|zh-tw|eng|jpn|kor|tc|sc)(?:\.|-|_|$)', name_body, re.IGNORECASE)
                if match:
                    lang_suffix = f".{match.group(1)}"

        # -------------------------------------------------
        # 2. 提取 Tags (关键修复步骤)
        # -------------------------------------------------
        tag_suffix = ""
        try:
            # 构造用于提取信息的“搜索名”
            search_name = original_name

            if is_sub:
                # 如果是字幕，把语言后缀和扩展名都去掉，伪装成纯视频文件名
                if lang_suffix and name_body.endswith(lang_suffix):
                    # 去掉 .zh
                    clean_body = name_body[:-len(lang_suffix)]
                    search_name = f"{clean_body}.mkv" # 补个假后缀防报错
                else:
                    # 如果没找到标准后缀，直接用 name_body
                    search_name = f"{name_body}.mkv"

            video_info = self._extract_video_info(search_name)
            if video_info:
                tag_suffix = f" · {video_info}"
        except Exception as e:
            # logger.debug(f"Tags提取失败: {e}")
            pass

        # -------------------------------------------------
        # 3. 构建新文件名
        # -------------------------------------------------
        if is_tv:
            # === 剧集模式 ===
            pattern = r'(?:s|S)(\d{1,2})(?:e|E)(\d{1,2})|Ep?(\d{1,2})|第(\d{1,3})[集话]'
            match = re.search(pattern, original_name)
            if match:
                s, e, ep_only, zh_ep = match.groups()
                season_num = int(s) if s else 1
                episode_num = int(e) if e else (int(ep_only) if ep_only else int(zh_ep))

                s_str = f"S{season_num:02d}"
                e_str = f"E{episode_num:02d}"

                # 格式：Title - S01E01 · Tags[.Lang].ext
                new_name = f"{new_base_name} - {s_str}{e_str}{tag_suffix}{lang_suffix}.{ext}"
                return new_name, season_num
            else:
                return original_name, None
        else:
            # === 电影模式 ===
            movie_base = f"{new_base_name} ({year})" if year else new_base_name

            # 格式：Title (Year) · Tags[.Lang].ext
            new_name = f"{movie_base}{tag_suffix}{lang_suffix}.{ext}"

            return new_name, None

    def _scan_files_recursively(self, cid, depth=0, max_depth=3):
        """递归扫描文件夹，返回所有文件的扁平列表"""
        all_files = []
        if depth > max_depth: return []

        try:
            # limit 调大一点，防止文件过多漏掉
            res = self.client.fs_files({'cid': cid, 'limit': 2000})
            if res.get('data'):
                for item in res['data']:
                    # 如果是文件 (有 fid)
                    if item.get('fid'):
                        all_files.append(item)
                    # 如果是文件夹 (无 fid)，且未达深度限制，递归
                    elif item.get('cid'):
                        sub_files = self._scan_files_recursively(item.get('cid'), depth + 1, max_depth)
                        all_files.extend(sub_files)
        except Exception as e:
            logger.warning(f"  ⚠️ 扫描目录出错 (CID: {cid}): {e}")

        return all_files

    def _is_junk_file(self, filename):
        """
        检查是否为垃圾文件/样本/花絮 (基于 MP 规则)
        """
        # 垃圾文件正则列表 (合并了通用规则和你提供的 MP 规则)
        junk_patterns = [
            # 基础关键词
            r'(?i)\b(sample|trailer|featurette|bonus)\b',

            # MP 规则集
            r'(?i)Special Ending Movie',
            r'(?i)\[((TV|BD|\bBlu-ray\b)?\s*CM\s*\d{2,3})\]',
            r'(?i)\[Teaser.*?\]',
            r'(?i)\[PV.*?\]',
            r'(?i)\[NC[OPED]+.*?\]',
            r'(?i)\[S\d+\s+Recap(\s+\d+)?\]',
            r'(?i)Menu',
            r'(?i)Preview',
            r'(?i)\b(CDs|SPs|Scans|Bonus|映像特典|映像|specials|特典CD|Menu|Logo|Preview|/mv)\b',
            r'(?i)\b(NC)?(Disc|片头|OP|SP|ED|Advice|Trailer|BDMenu|片尾|PV|CM|Preview|MENU|Info|EDPV|SongSpot|BDSpot)(\d{0,2}|_ALL)\b',
            r'(?i)WiKi\.sample'
        ]

        for pattern in junk_patterns:
            if re.search(pattern, filename):
                return True
        return False

    def execute(self, root_item, target_cid, webhook=False):
        """
        执行整理：先尝试创建，失败后再查找（高效率模式），且一步到位移动
        """
        # 1. 准备标准名称
        title = self.details.get('title') or self.original_title
        date_str = self.details.get('date') or ''
        year = date_str[:4] if date_str else ''

        safe_title = re.sub(r'[\\/:*?"<>|]', '', title).strip()
        std_root_name = f"{safe_title} ({year}) {{tmdb={self.tmdb_id}}}" if year else f"{safe_title} {{tmdb={self.tmdb_id}}}"

        source_root_id = root_item.get('fid') or root_item.get('cid')
        is_source_file = bool(root_item.get('fid'))
        dest_parent_cid = target_cid if (target_cid and str(target_cid) != '0') else root_item.get('cid')

        config = get_config()
        configured_exts = config.get(constants.CONFIG_OPTION_115_EXTENSIONS, [])
        allowed_exts = set(e.lower() for e in configured_exts)
        known_video_exts = {'mp4', 'mkv', 'avi', 'ts', 'iso', 'rmvb', 'wmv', 'mov', 'm2ts', 'flv', 'mpg'}
        MIN_VIDEO_SIZE = 10 * 1024 * 1024

        logger.info(f"  🚀 [115] 开始整理: {root_item.get('n')} -> {std_root_name}")

        # ==================================================
        # 步骤 A: 获取主目录 CID (★ 纯净增强版：先DB -> 再创建 -> 搜索 -> 暴力翻页)
        # ==================================================
        final_home_cid = P115CacheManager.get_cid(dest_parent_cid, std_root_name)

        if final_home_cid:
            logger.info(f"  ⚡ [缓存命中] 主目录: {std_root_name}")
        else:
            # 1. 缓存没命中，直接尝试创建
            mk_res = self.client.fs_mkdir(std_root_name, dest_parent_cid)
            if mk_res.get('state'):
                final_home_cid = mk_res.get('cid')
                P115CacheManager.save_cid(final_home_cid, dest_parent_cid, std_root_name)
                logger.info(f"  🆕 创建新主目录并缓存: {std_root_name}")
            else:
                # 2. 创建失败（目录已存在），尝试使用 115 的 search_value
                try:
                    search_res = self.client.fs_files({'cid': dest_parent_cid, 'search_value': std_root_name, 'limit': 1150})
                    if search_res.get('data'):
                        for item in search_res['data']:
                            if item.get('n') == std_root_name and not item.get('fid'):
                                final_home_cid = item.get('cid')
                                P115CacheManager.save_cid(final_home_cid, dest_parent_cid, std_root_name) # ★ 只在这里存
                                logger.info(f"  📂 成功查找到已存在主目录并永久缓存: {std_root_name}")
                                break
                except Exception as e:
                    logger.warning(f"  ⚠️ 115模糊查找异常: {e}")

                # 3. ★★★ 终极暴力兜底 ★★★：如果搜索真瞎了，手工翻页遍历找！
                if not final_home_cid:
                    logger.warning(f"  ⚠️ 115搜索失效，启动全量遍历查找老目录: '{std_root_name}' ...")
                    offset = 0
                    limit = 1000
                    while True:
                        try:
                            res = self.client.fs_files({'cid': dest_parent_cid, 'limit': limit, 'offset': offset, 'type': 0})
                            data = res.get('data', [])
                            if not data: break # 翻到底了
                            
                            for item in data:
                                if item.get('n') == std_root_name:
                                    final_home_cid = item.get('cid')
                                    P115CacheManager.save_cid(final_home_cid, dest_parent_cid, std_root_name) # ★ 只在这里存
                                    logger.info(f"  📂 成功查找到已存在主目录并永久缓存: {std_root_name}")
                                    break
                                    
                            if final_home_cid: break # 找到了
                            
                            offset += limit # 准备查下一页
                        except Exception as e:
                            logger.error(f"遍历查找失败: {e}")
                            break

        if not final_home_cid:
            logger.error(f"  ❌ 无法获取或创建目标目录 (已尝试所有手段)")
            return False

        # ==================================================
        # 步骤 B: 扫描源文件
        # ==================================================
        candidates = []
        if is_source_file:
            candidates.append(root_item)
        else:
            candidates = self._scan_files_recursively(source_root_id, max_depth=3)

        if not candidates: return True

        # ==================================================
        # 步骤 C: 处理文件
        # ==================================================
        moved_count = 0
        for file_item in candidates:
            fid = file_item.get('fid')
            file_name = file_item.get('n', '')
            ext = file_name.split('.')[-1].lower() if '.' in file_name else ''

            if self._is_junk_file(file_name): continue
            if ext not in allowed_exts: continue
            
            file_size = _parse_115_size(file_item.get('s') or file_item.get('size'))
            if ext in known_video_exts and 0 < file_size < MIN_VIDEO_SIZE: continue

            # 1. 重命名计算
            new_filename, season_num = self._rename_file_node(
                file_item, safe_title, year=year, is_tv=(self.media_type=='tv')
            )

            # 2. 提前确定最终目的地（季目录：先创建后查找逻辑）
            real_target_cid = final_home_cid
            if self.media_type == 'tv' and season_num is not None:
                s_name = f"Season {season_num:02d}"
                
                # ★ 改用 DB 缓存
                s_cid = P115CacheManager.get_cid(final_home_cid, s_name)
                
                if s_cid:
                    logger.info(f"  ⚡ [缓存命中] 季目录: {std_root_name} - {s_name}")
                    real_target_cid = s_cid
                else:
                    # 尝试创建
                    s_mk = self.client.fs_mkdir(s_name, final_home_cid)
                    s_cid = s_mk.get('cid') if s_mk.get('state') else None
                    
                    if not s_cid: # 创建失败，查找
                        try:
                            s_search = self.client.fs_files({'cid': final_home_cid, 'search_value': s_name, 'limit': 1150})
                            for item in s_search.get('data', []):
                                if item.get('n') == s_name and not item.get('fid'):
                                    s_cid = item.get('cid')
                                    break
                        except: pass
                    
                    if s_cid:
                        P115CacheManager.save_cid(s_cid, final_home_cid, s_name)
                        logger.info(f"  🆕 创建季目录并缓存: {std_root_name} - {s_name}")
                        real_target_cid = s_cid

            # 3. 先改名
            if new_filename != file_name and webhook == False:
                if self.client.fs_rename((fid, new_filename)).get('state'):
                    logger.info(f"  ✏️ [重命名] {file_name} -> {new_filename}")
            else:
                logger.info(f"  ✏️ [MP上传] 跳过重命名 (已是标准名): {file_name}")

            # 4. 一步到位移动到目的地
            if self.client.fs_move(fid, real_target_cid).get('state'):
                if self.media_type == 'tv' and season_num is not None:
                    logger.info(f"  📁 [移动] {file_name} -> {std_root_name} - {s_name}")
                else:
                    logger.info(f"  📁 [移动] {file_name} -> {std_root_name}")
                moved_count += 1

                # ==================================================
                # ★★★ 终极形态：同步生成本地 .strm 直链文件 ★★★
                # ==================================================
                pick_code = file_item.get('pc')  # 115 文件的提取码
                local_root = config.get(constants.CONFIG_OPTION_LOCAL_STRM_ROOT)
                etk_url = config.get(constants.CONFIG_OPTION_ETK_SERVER_URL, "http://127.0.0.1:5257").rstrip('/')
                
                if pick_code and local_root and os.path.exists(local_root):
                    try:
                        # 1. 获取当前匹配到的分类目录名 (如 "欧美电影")
                        category_name = None
                        for rule in self.rules:
                            if rule.get('cid') == str(target_cid):
                                category_name = rule.get('dir_name', '未识别')
                                break
                        if not category_name: category_name = "未识别"

                        # 2. 拼接本地绝对路径
                        media_root_cid = str(config.get(constants.CONFIG_OPTION_115_MEDIA_ROOT_CID, '0'))
                        
                        # 使用类变量做内存缓存，避免对同一个分类目录反复请求 115 接口
                        if not hasattr(self.__class__, '_category_path_cache'):
                            self.__class__._category_path_cache = {}
                            
                        if str(target_cid) not in self.__class__._category_path_cache:
                            try:
                                # 极速请求一次目标目录的信息，115 会返回完整的父级链路 path
                                dir_info = self.client.fs_files({'cid': target_cid, 'limit': 1})
                                path_nodes = dir_info.get('path', [])
                                
                                start_idx = 0
                                found_root = False
                                
                                # 在链路中寻找用户配置的“媒体库根目录”
                                if media_root_cid == '0':
                                    start_idx = 1 # 跳过 115 的物理根目录 "根目录"
                                    found_root = True
                                else:
                                    for i, node in enumerate(path_nodes):
                                        if str(node.get('cid')) == media_root_cid:
                                            start_idx = i + 1 # 从根目录的下一级开始取
                                            found_root = True
                                            break
                                
                                if found_root and start_idx < len(path_nodes):
                                    # 完美提取中间所有的层级！例如: ['动漫', '连载中', '热血']
                                    rel_segments = [str(n.get('name')).strip() for n in path_nodes[start_idx:]]
                                    self.__class__._category_path_cache[str(target_cid)] = os.path.join(*rel_segments)
                                else:
                                    # 兜底：如果层级异常，用规则里配的名称
                                    fallback_name = next((r.get('dir_name') for r in self.rules if str(r.get('cid')) == str(target_cid)), "未识别")
                                    self.__class__._category_path_cache[str(target_cid)] = fallback_name
                            except Exception as e:
                                logger.warning(f"获取目录路径层级失败: {e}")
                                self.__class__._category_path_cache[str(target_cid)] = "未识别"

                        # 拿到完美对应的相对路径 (例如: "纪录片/BBC")
                        relative_category_path = self.__class__._category_path_cache[str(target_cid)]

                        # 2. 拼接本地绝对路径 (现在它和 115 网盘的层级 100% 对应了！)
                        if self.media_type == 'tv' and season_num is not None:
                            local_dir = os.path.join(local_root, relative_category_path, std_root_name, s_name)
                        else:
                            local_dir = os.path.join(local_root, relative_category_path, std_root_name)
                        
                        os.makedirs(local_dir, exist_ok=True) # 自动创建本地文件夹结构

                        # 3. 构造 strm 文件名和直链内容
                        ext = new_filename.split('.')[-1].lower() if '.' in new_filename else ''
                        is_video = ext in known_video_exts
                        is_sub = ext in ['srt', 'ass', 'ssa', 'sub', 'vtt', 'sup']

                        if is_video:
                            # 处理视频 -> 生成 1KB 的 .strm 文件
                            strm_filename = os.path.splitext(new_filename)[0] + ".strm"
                            strm_filepath = os.path.join(local_dir, strm_filename)
                            strm_content = f"{etk_url}/api/p115/play/{pick_code}"
                            
                            with open(strm_filepath, 'w', encoding='utf-8') as f:
                                f.write(strm_content)
                            logger.info(f"  📝 STRM 已生成 -> {strm_filename}")
                            
                        elif is_sub:
                            # 检查是否开启了字幕下载开关
                            if config.get(constants.CONFIG_OPTION_115_DOWNLOAD_SUBS, True):
                                # 处理字幕 -> 真实下载到本地供 Emby 挂载
                                sub_filepath = os.path.join(local_dir, new_filename)
                                if not os.path.exists(sub_filepath):
                                    try:
                                        logger.info(f"  ⬇️ [字幕下载] 正在向 115 拉取外挂字幕: {new_filename} ...")
                                        # 索取直链
                                        url_obj = self.client.download_url(pick_code, user_agent="Mozilla/5.0")
                                        dl_url = str(url_obj)
                                        if dl_url:
                                            import requests
                                            # ★ 修复 403：必须带上伪装的 UA 和 115 的 Cookie
                                            headers = {
                                                "User-Agent": "Mozilla/5.0",
                                                "Cookie": self.get_cookies()
                                            }
                                            resp = requests.get(dl_url, stream=True, timeout=30, headers=headers)
                                            resp.raise_for_status()
                                            with open(sub_filepath, 'wb') as f:
                                                for chunk in resp.iter_content(chunk_size=8192):
                                                    f.write(chunk)
                                            logger.info(f"  ✅ [字幕下载] 下载完成！")
                                    except Exception as e:
                                        logger.error(f"  ❌ 下载字幕失败: {e}")
                        
                    except Exception as e:
                        logger.error(f"  ❌ 生成 STRM 文件失败: {e}", exc_info=True)

        # ==================================================
        # 步骤 D: 清理源目录
        # ==================================================
        if not is_source_file and moved_count > 0:
            self.client.fs_delete([source_root_id])
            logger.info(f"  🧹 已清理空目录")

        return True

def _parse_115_size(size_val):
    """
    统一解析 115 返回的文件大小为字节(Int)
    支持: 12345(int), "12345"(str), "1.2GB", "500KB"
    """
    try:
        if size_val is None: return 0

        # 1. 如果已经是数值 (115 API 's' 字段通常是 int)
        if isinstance(size_val, (int, float)):
            return int(size_val)

        # 2. 如果是字符串
        if isinstance(size_val, str):
            s = size_val.strip()
            if not s: return 0
            # 纯数字字符串
            if s.isdigit():
                return int(s)

            s_upper = s.upper().replace(',', '')
            mult = 1
            if 'TB' in s_upper: mult = 1024**4
            elif 'GB' in s_upper: mult = 1024**3
            elif 'MB' in s_upper: mult = 1024**2
            elif 'KB' in s_upper: mult = 1024

            match = re.search(r'([\d\.]+)', s_upper)
            if match:
                return int(float(match.group(1)) * mult)
    except Exception:
        pass
    return 0

def get_115_account_info():
    """
    极简状态检查：只验证 Cookie 是否有效，不获取任何详情
    """
    client = P115Service.get_client()
    if not client: raise Exception("无法初始化 115 客户端")

    config = get_config()
    cookies = config.get(constants.CONFIG_OPTION_115_COOKIES)

    if not cookies:
        raise Exception("未配置 Cookies")

    try:
        # 尝试列出 1 个文件，这是验证 Cookie 最快最准的方法
        resp = client.fs_files({'limit': 1})

        if not resp.get('state'):
            raise Exception("Cookie 已失效")

        # 只要没报错，就是有效
        return {
            "valid": True,
            "msg": "115 状态正常，Cookie 有效"
        }

    except Exception as e:
        raise Exception("Cookie 无效或网络不通")


def _identify_media_enhanced(filename, forced_media_type=None):
    """
    增强识别逻辑：
    1. 支持多种 TMDb ID 标签格式: {tmdb=xxx}
    2. 支持标准命名格式: Title (Year)
    3. 接收外部强制指定的类型 (forced_media_type)，不再轮询猜测
    
    返回: (tmdb_id, media_type, title) 或 (None, None, None)
    """
    tmdb_id = None
    media_type = 'movie' # 默认
    title = filename
    
    # 1. 优先提取 TMDb ID 标签 (最稳)
    match_tag = re.search(r'\{?tmdb(?:id)?[=\-](\d+)\}?', filename, re.IGNORECASE)
    
    if match_tag:
        tmdb_id = match_tag.group(1)
        
        # 如果外部指定了类型，直接用；否则看文件名特征
        if forced_media_type:
            media_type = forced_media_type
        elif re.search(r'(?:S\d{1,2}|E\d{1,2}|第\d+季|Season)', filename, re.IGNORECASE):
            media_type = 'tv'
        
        # 提取标题
        clean_name = re.sub(r'\{?tmdb(?:id)?[=\-]\d+\}?', '', filename, flags=re.IGNORECASE).strip()
        match_title = re.match(r'^(.+?)\s*[\(\[]\d{4}[\)\]]', clean_name)
        if match_title:
            title = match_title.group(1).strip()
        else:
            title = clean_name
            
        return tmdb_id, media_type, title

    # 2. 其次提取标准格式 Title (Year)
    match_std = re.match(r'^(.+?)\s+[\(\[](\d{4})[\)\]]', filename)
    if match_std:
        name_part = match_std.group(1).strip()
        year_part = match_std.group(2)
        
        # === 关键修正：类型判断逻辑 ===
        if forced_media_type:
            # 如果外部透视过目录，确定是 TV，直接信赖
            media_type = forced_media_type
        else:
            # 否则才根据文件名特征判断
            if re.search(r'(?:S\d{1,2}|E\d{1,2}|第\d+季|Season)', filename, re.IGNORECASE):
                media_type = 'tv'
            else:
                media_type = 'movie'
            
        # 尝试通过 TMDb API 确认 ID
        try:
            api_key = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_TMDB_API_KEY)
            if api_key:
                # 精准搜索，不轮询，不瞎猜
                results = tmdb.search_media(
                    query=name_part, 
                    api_key=api_key, 
                    item_type=media_type, 
                    year=year_part
                )
                
                if results and len(results) > 0:
                    best = results[0]
                    return best['id'], media_type, (best.get('title') or best.get('name'))
                else:
                    logger.warning(f"  ⚠️ TMDb 未找到资源: {name_part} ({year_part}) 类型: {media_type}")

        except Exception as e:
            pass

    return None, None, None


def task_scan_and_organize_115(processor=None):
    """
    [任务链] 主动扫描 115 待整理目录
    - 识别成功 -> 归类到目标目录
    - 识别失败 -> 移动到 '未识别' 目录
    ★ 修复：增加子文件探测逻辑，防止剧集文件夹因命名不规范被误判为电影
    """
    logger.info("=== 开始执行 115 待整理目录扫描 ===")

    client = P115Service.get_client()
    if not client: raise Exception("无法初始化 115 客户端")

    config = get_config()
    cookies = config.get(constants.CONFIG_OPTION_115_COOKIES)
    cid_val = config.get(constants.CONFIG_OPTION_115_SAVE_PATH_CID)
    save_val = config.get(constants.CONFIG_OPTION_115_SAVE_PATH_NAME, '待整理')
    enable_organize = config.get(constants.CONFIG_OPTION_115_ENABLE_ORGANIZE, False)

    if not cookies:
        logger.error("  ⚠️ 未配置 115 Cookies，跳过。")
        return
    if not cid_val or str(cid_val) == '0':
        logger.error("  ⚠️ 未配置待整理目录 (CID)，跳过。")
        return
    if not enable_organize:
        logger.warning("  ⚠️ 未开启智能整理开关，仅扫描不处理。")
        return

    try:
        save_cid = int(cid_val)
        save_name = str(save_val)

        # 1. 准备 '未识别' 目录 
        unidentified_folder_name = "未识别"
        unidentified_cid = None
        try:
            search_res = client.fs_files({'cid': save_cid, 'search_value': unidentified_folder_name, 'limit': 1})
            if search_res.get('data'):
                for item in search_res['data']:
                    if item.get('n') == unidentified_folder_name and (item.get('ico') == 'folder' or not item.get('fid')):
                        unidentified_cid = item.get('cid')
                        break
        except: pass

        if not unidentified_cid:
            try:
                mk_res = client.fs_mkdir(unidentified_folder_name, save_cid)
                if mk_res.get('state'):
                    unidentified_cid = mk_res.get('cid')
            except: pass

        # 2. 扫描目录
        logger.info(f"  🔍 正在扫描目录: {save_name} ...")
        res = client.fs_files({'cid': save_cid, 'limit': 50, 'o': 'user_ptime', 'asc': 0})

        if not res.get('data'):
            logger.info(f"  📂 [{save_name}] 目录为空。")
            return

        processed_count = 0
        moved_to_unidentified = 0

        for item in res['data']:
            name = item.get('n')
            item_id = item.get('fid') or item.get('cid')
            is_folder = not item.get('fid') # 判断是否为文件夹

            if str(item_id) == str(unidentified_cid) or name == unidentified_folder_name:
                continue

            forced_type = None
            if is_folder:
                try:
                    # 偷看一眼文件夹里面的内容 (取前20个足矣)
                    sub_res = client.fs_files({'cid': item.get('cid'), 'limit': 20})
                    if sub_res.get('data'):
                        for sub_item in sub_res['data']:
                            sub_name = sub_item.get('n', '')
                            # 只要包含 Season XX, S01, EP01, 第X季，就是电视剧
                            # 你的截图里是 "Season 01"，这个正则能完美匹配
                            if re.search(r'(Season\s?\d+|S\d+|Ep?\d+|第\d+季)', sub_name, re.IGNORECASE):
                                forced_type = 'tv'
                                logger.info(f"  🕵️‍♂️ [结构探测] 目录 '{name}' 包含子项 '{sub_name}' -> 判定为 TV")
                                break
                except Exception as e:
                    logger.warning(f"  ⚠️ 目录透视失败: {e}")

            # 3. 识别 (传入 forced_type)
            tmdb_id, media_type, title = _identify_media_enhanced(name, forced_media_type=forced_type)
            
            if tmdb_id:
                logger.info(f"  ➜ 识别成功: {name} -> ID:{tmdb_id} ({media_type})")
                try:
                    # 4. 归类
                    organizer = SmartOrganizer(client, tmdb_id, media_type, title)
                    target_cid = organizer.get_target_cid()
                    if organizer.execute(item, target_cid):
                        processed_count += 1
                except Exception as e:
                    logger.error(f"  ❌ 整理出错: {e}")
            else:
                # 5. 识别失败 -> 移动到 '未识别'
                if unidentified_cid:
                    logger.info(f"  ⚠️ 无法识别: {name} -> 移动到 '未识别'")
                    try:
                        client.fs_move(item_id, unidentified_cid)
                        moved_to_unidentified += 1
                    except: pass

        logger.info(f"=== 扫描结束，成功归类 {processed_count} 个，移入未识别 {moved_to_unidentified} 个 ===")

    except Exception as e:
        logger.error(f"  ⚠️ 115 扫描任务异常: {e}", exc_info=True)

def task_sync_115_directory_tree(processor=None):
    """
    主动同步 115 分类目录下的所有子目录到本地 DB 缓存。
    这能彻底解决 115 API search_value 失效导致的老目录无法识别问题。
    """
    logger.info("=== 开始全量同步 115 目录树到本地数据库 ===")
    
    # 局部导入 task_manager 用于向前端发送实时进度 (防止与 core.py 循环引用)
    try:
        import task_manager
    except ImportError:
        task_manager = None

    def update_progress(prog, msg):
        if task_manager:
            task_manager.update_status_from_thread(prog, msg)
        logger.info(msg)

    client = P115Service.get_client()
    if not client: 
        update_progress(100, "115 客户端未初始化，任务结束。")
        return

    raw_rules = settings_db.get_setting(constants.DB_KEY_115_SORTING_RULES)
    if not raw_rules: 
        update_progress(100, "未配置分类规则，无需同步。")
        return
    
    rules = json.loads(raw_rules) if isinstance(raw_rules, str) else raw_rules
    
    # 提取所有启用的规则中的目标分类目录 CID，并去重
    target_cids = set()
    for rule in rules:
        if rule.get('enabled', True) and rule.get('cid'):
            cid_str = str(rule['cid'])
            if cid_str and cid_str != '0':
                target_cids.add(cid_str)

    if not target_cids:
        update_progress(100, "未找到有效的分类目标目录 CID，任务结束。")
        return

    total_cached = 0
    total_cids = len(target_cids)
    
    for idx, cid in enumerate(target_cids):
        base_prog = int((idx / total_cids) * 100)
        update_progress(base_prog, f"  🔍 正在扫描第 {idx+1}/{total_cids} 个分类目录 (CID: {cid})...")
        
        offset = 0
        limit = 1000
        page_count = 0
        
        while True:
            # 响应前端的中止任务按钮
            if processor and getattr(processor, 'is_stop_requested', lambda: False)():
                update_progress(100, "任务已被用户手动终止。")
                return

            try:
                # 获取数据列表
                res = client.fs_files({'cid': cid, 'limit': limit, 'offset': offset})
                data = res.get('data', [])
                
                if not data: 
                    break # 本目录全空，跳出
                
                page_count += 1
                dir_count_in_page = 0
                
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        for item in data:
                            # ★ 核心：没有 fid 的项目才是文件夹
                            if not item.get('fid'):
                                sub_cid = item.get('cid')
                                sub_name = item.get('n')
                                if sub_cid and sub_name:
                                    cursor.execute("""
                                        INSERT INTO p115_filesystem_cache (id, parent_id, name)
                                        VALUES (%s, %s, %s)
                                        ON CONFLICT (parent_id, name)
                                        DO UPDATE SET id = EXCLUDED.id, updated_at = NOW()
                                    """, (str(sub_cid), str(cid), str(sub_name)))
                                    total_cached += 1
                                    dir_count_in_page += 1
                        conn.commit()
                
                # 实时播报当前正在翻第几页，以及入库了多少个文件夹
                update_progress(base_prog, f"  ➜ CID: {cid} | 翻阅第 {page_count} 页 | 新增/更新 {dir_count_in_page} 个目录...")
                
                # ★ 性能优化：如果获取的数据小于请求的上限，说明到底了，不用再请求下一页
                if len(data) < limit:
                    break
                    
                offset += limit
                time.sleep(1) # 稍微喘口气，防 115 踢人
                
            except Exception as e:
                logger.error(f"  ❌ 同步目录树异常 (CID: {cid}): {e}")
                break # 发生异常，跳过这个 CID 继续查下一个

    update_progress(100, f"=== 同步结束！共成功更新 {total_cached} 个目录的缓存 ===")

def task_full_sync_strm_and_subs(processor=None):
    """
    极速全量生成 STRM 与 同步字幕 (带防失败自动降级机制)
    修复版：完美对齐网盘与本地分类目录的层级路径
    """
    config = get_config()
    download_subs = config.get(constants.CONFIG_OPTION_115_DOWNLOAD_SUBS, True)
    enable_cleanup = config.get(constants.CONFIG_OPTION_115_LOCAL_CLEANUP, False)
    start_msg = "=== 🚀 开始全量生成 STRM 与 同步字幕 ===" if download_subs else "=== 🚀 开始全量生成 STRM (已跳过字幕) ==="
    if enable_cleanup: start_msg += " [已开启本地清理]"
    logger.info(start_msg)
    
    try:
        import task_manager
    except ImportError:
        task_manager = None

    def update_progress(prog, msg):
        if task_manager: task_manager.update_status_from_thread(prog, msg)
        logger.info(msg)

    local_root = config.get(constants.CONFIG_OPTION_LOCAL_STRM_ROOT)
    etk_url = config.get(constants.CONFIG_OPTION_ETK_SERVER_URL, "").rstrip('/')
    media_root_cid = str(config.get(constants.CONFIG_OPTION_115_MEDIA_ROOT_CID, '0'))
    
    known_video_exts = {'mp4', 'mkv', 'avi', 'ts', 'iso', 'rmvb', 'wmv', 'mov', 'm2ts', 'flv', 'mpg'}
    known_sub_exts = {'srt', 'ass', 'ssa', 'sub', 'vtt', 'sup'}
    
    allowed_exts = set(e.lower() for e in config.get(constants.CONFIG_OPTION_115_EXTENSIONS, []))
    if not allowed_exts:
        allowed_exts = known_video_exts | known_sub_exts
    
    if not local_root or not etk_url:
        update_progress(100, "错误：未配置本地 STRM 根目录或 ETK 访问地址！")
        return

    client = P115Service.get_client()
    if not client: return

    raw_rules = settings_db.get_setting(constants.DB_KEY_115_SORTING_RULES)
    if not raw_rules: return
    rules = json.loads(raw_rules) if isinstance(raw_rules, str) else raw_rules
    
    # 1. 预处理：获取每个目标分类目录对应的完整相对路径 (参考 execute 逻辑)
    cid_to_rel_path = {}
    target_cids = []
    
    for r in rules:
        if r.get('enabled', True) and r.get('cid') and str(r['cid']) != '0':
            cid = str(r['cid'])
            target_cids.append(cid)
            try:
                # 获取该目录的完整链路信息
                dir_info = client.fs_files({'cid': cid, 'limit': 1})
                path_nodes = dir_info.get('path', [])
                
                start_idx = 0
                found_root = False
                
                # 在链路中寻找“媒体库根目录”
                if media_root_cid == '0':
                    start_idx = 1 # 跳过网盘物理“根目录”
                    found_root = True
                else:
                    for i, node in enumerate(path_nodes):
                        if str(node.get('cid')) == media_root_cid:
                            start_idx = i + 1
                            found_root = True
                            break
                
                if found_root and start_idx < len(path_nodes):
                    # 提取中间所有层级，例如: ['电影', '欧美电影']
                    rel_segments = [str(n.get('name')).strip() for n in path_nodes[start_idx:]]
                    cid_to_rel_path[cid] = os.path.join(*rel_segments)
                else:
                    # 兜底：使用规则中配置的名称
                    cid_to_rel_path[cid] = r.get('dir_name', '未识别')
            except Exception as e:
                logger.warning(f"获取 CID:{cid} 路径层级失败: {e}")
                cid_to_rel_path[cid] = r.get('dir_name', '未识别')

    valid_local_files = set() # 本地已存在的 STRM 和字幕文件绝对路径集合（仅当 enable_cleanup=True 时使用）
    successful_cids = set() # 记录成功处理过的 CID，最后用于清理本地多余文件
    # ==========================================
    # ★ 内部处理逻辑：接收 base_cid 来确定分类前缀
    # ==========================================
    def process_file_info(info, rel_path_parts, base_cid):
        nonlocal files_generated
        name = info.get('name') or info.get('n', '')
        ext = name.split('.')[-1].lower() if '.' in name else ''
        if ext not in allowed_exts: return
        
        pc = info.get('pc') or info.get('pickcode')
        if not pc: return
        
        # 获取分类前缀路径 (例如 "纪录片/BBC")
        category_prefix = cid_to_rel_path.get(str(base_cid), "未识别")
        
        # 拼接本地路径：本地根目录 / 分类前缀 / 资源子目录 / 文件
        current_local_path = os.path.join(local_root, category_prefix, *rel_path_parts)
        os.makedirs(current_local_path, exist_ok=True)
        
        if ext in known_video_exts:
            strm_name = os.path.splitext(name)[0] + ".strm"
            strm_path = os.path.join(current_local_path, strm_name)
            content = f"{etk_url}/api/p115/play/{pc}"
            
            need_write = True
            if os.path.exists(strm_path):
                try:
                    with open(strm_path, 'r', encoding='utf-8') as f:
                        if f.read().strip() == content: need_write = False
                except: pass
                        
            if need_write:
                with open(strm_path, 'w', encoding='utf-8') as f: f.write(content)
                logger.debug(f"生成 STRM: {strm_name}")
            files_generated += 1
            valid_local_files.add(os.path.abspath(strm_path)) # 记录有效文件绝对路径
                
        elif ext in known_sub_exts:
            # 检查开关
            if download_subs:
                sub_path = os.path.join(current_local_path, name)
                if not os.path.exists(sub_path):
                    try:
                        import requests
                        url_obj = client.download_url(pc, user_agent="Mozilla/5.0")
                        if url_obj:
                            headers = {
                                "User-Agent": "Mozilla/5.0",
                                "Cookie": P115Service.get_cookies()
                            }
                            resp = requests.get(str(url_obj), stream=True, timeout=15, headers=headers)
                            resp.raise_for_status()
                            with open(sub_path, 'wb') as f:
                                for chunk in resp.iter_content(8192): f.write(chunk)
                            logger.info(f"下载字幕: {name}")
                        files_generated += 1
                        valid_local_files.add(os.path.abspath(sub_path)) # 记录有效文件绝对路径
                    except Exception as e:
                        logger.error(f"下载字幕失败 [{name}]: {e}")

    # ==========================================
    # 2. 遍历执行
    # ==========================================
    total_cids = len(target_cids)
    for idx, base_cid in enumerate(target_cids):
        base_prog = int((idx / total_cids) * 100)
        category_rel_path = cid_to_rel_path.get(base_cid)
        update_progress(base_prog, f"  ➜ 正在同步层级: {category_rel_path} (CID: {base_cid}) ...")
        
        items_yielded = 0
        files_generated = 0
        
        # A. 优先尝试极速遍历
        try:
            from p115client.tool.iterdir import iter_files_with_path_skim
            
            iterator = iter_files_with_path_skim(
                client, 
                int(base_cid), 
                with_ancestors=True, 
                max_workers=1 
            )
            
            for info in iterator:
                if processor and getattr(processor, 'is_stop_requested', lambda: False)():
                    update_progress(100, "任务已被用户手动终止。")
                    return
                
                # 只有带 fid 的才是文件，文件夹不参与 process_file_info
                fid = info.get('fid') or info.get('id')
                if not fid or info.get('ico') == 'folder':
                    continue

                items_yielded += 1
                
                ancestors = info.get('ancestors', [])
                rel_path_parts = []
                
                if isinstance(ancestors, list) and len(ancestors) > 0:
                    found_base = False
                    for node in ancestors:
                        node_id = str(node.get('id') or node.get('cid', ''))
                        
                        # 找到规则配置的根 CID
                        if node_id == str(base_cid):
                            found_base = True
                            continue
                        
                        if found_base:
                            # 修复点 1：确保这个节点不是文件本身（防止极速模式把文件当路径）
                            node_name = str(node.get('name', '')).strip()
                            if node_id != str(fid) and node_name:
                                rel_path_parts.append(node_name)
                
                # 修复点 2：双重保险。如果路径最后一位跟文件名完全一样（比如 115 里的特殊打包文件），剔除它
                file_real_name = info.get('n') or info.get('name', '')
                if rel_path_parts and rel_path_parts[-1] == file_real_name:
                    rel_path_parts.pop()

                process_file_info(info, rel_path_parts, base_cid)
                
        except Exception as e:
            logger.warning(f"  ⚠️ 极速遍历异常 CID:{base_cid} - 错误详情: {repr(e)}")

        # B. 自动降级：如果极速模式没出货，启动标准递归
        if items_yielded == 0:
            logger.warning(f"  ⚠️ 极速遍历未发现文件，正在使用标准递归扫描...")
            def reliable_recursive_scan(cid, current_parts):
                offset = 0
                limit = 1000
                while True:
                    res = client.fs_files({'cid': cid, 'limit': limit, 'offset': offset})
                    data = res.get('data', [])
                    if not data: break
                    for item in data:
                        if item.get('fid'):
                            process_file_info(item, current_parts, base_cid)
                        else:
                            reliable_recursive_scan(item.get('cid'), current_parts + [item.get('n')])
                    if len(data) < limit: break
                    offset += limit
            
            try:
                reliable_recursive_scan(base_cid, [])
            except Exception as e:
                logger.error(f"标准扫描异常 CID:{base_cid}: {e}")
                
        logger.info(f"  ✅ [{category_rel_path}] 同步完成，处理文件: {files_generated}")
        if files_generated > 0:
            successful_cids.add(base_cid)
        # ==========================================
    # ★ 新增：安全的本地清理逻辑 (放在 for 循环外面，函数的末尾)
    # ==========================================
    if enable_cleanup:
        update_progress(95, "  🧹 正在执行本地多余文件清理...")
        cleaned_files = 0
        cleaned_dirs = 0
        
        for base_cid in successful_cids:
            category_rel_path = cid_to_rel_path.get(base_cid)
            target_local_dir = os.path.join(local_root, category_rel_path)
            
            if not os.path.exists(target_local_dir): continue
            
            # 1. 清理多余的文件 (只碰 strm 和 字幕)
            for root_dir, dirs, files in os.walk(target_local_dir):
                for file in files:
                    ext = file.split('.')[-1].lower()
                    if ext in known_sub_exts or ext == 'strm':
                        file_path = os.path.abspath(os.path.join(root_dir, file))
                        if file_path not in valid_local_files:
                            try:
                                os.remove(file_path)
                                cleaned_files += 1
                                logger.debug(f"  🗑️ [清理] 删除失效文件: {file}")
                            except Exception as e:
                                logger.warning(f"  ⚠️ 删除文件失败 {file}: {e}")
            
            # 2. 清理空文件夹 (自底向上)
            for root_dir, dirs, files in os.walk(target_local_dir, topdown=False):
                for d in dirs:
                    dir_path = os.path.join(root_dir, d)
                    try:
                        if not os.listdir(dir_path): # 如果文件夹为空
                            os.rmdir(dir_path)
                            cleaned_dirs += 1
                    except: pass
                    
        logger.info(f"  🧹 清理完成: 删除了 {cleaned_files} 个失效文件, {cleaned_dirs} 个空目录。")

    end_msg = "=== 全量 STRM 与字幕同步结束 ===" if download_subs else "=== 全量 STRM 生成结束 ==="
    update_progress(100, end_msg)

def delete_115_files_by_webhook(item_path, pickcodes):
    """
    接收神医 Webhook 传来的路径和提取码，精准销毁 115 网盘文件。
    """
    if not pickcodes or not item_path: return

    client = P115Service.get_client()
    if not client: return

    try:
        # 1. 从本地路径中提取带有 TMDb ID 的主目录名称 (例如: 爱我爱我 (2026) {tmdb=1317672})
        match = re.search(r'([^/\\]+\{tmdb=\d+\})', item_path)
        if not match:
            logger.warning(f"  ⚠️ [联动删除] 无法从路径提取 TMDb 目录名: {item_path}")
            return
        tmdb_folder_name = match.group(1)

        # 2. 查找该主目录在 115 上的 CID
        base_cid = P115CacheManager.get_cid_by_name(tmdb_folder_name)
        if not base_cid:
            # 缓存没命中，尝试模糊搜索兜底
            res = client.fs_files({'search_value': tmdb_folder_name, 'limit': 10})
            for item in res.get('data', []):
                if item.get('n') == tmdb_folder_name and not item.get('fid'):
                    base_cid = item.get('cid')
                    break

        if not base_cid:
            logger.warning(f"  ⚠️ [联动删除] 未在 115 找到对应主目录，可能已被删除: {tmdb_folder_name}")
            return

        # 3. 递归扫描该主目录，将 Pickcode 映射为 115 的文件 ID (fid)
        fids_to_delete = []
        
        def scan_and_match(cid):
            res = client.fs_files({'cid': cid, 'limit': 1000})
            for item in res.get('data', []):
                if item.get('fid'):
                    # 如果文件的提取码在我们要删除的列表中
                    if item.get('pc') in pickcodes:
                        fids_to_delete.append(item.get('fid'))
                elif item.get('cid'):
                    scan_and_match(item.get('cid'))

        logger.debug(f"  🔍 [联动删除] 正在网盘目录 '{tmdb_folder_name}' 中匹配文件...")
        scan_and_match(base_cid)

        # 4. 执行物理销毁
        if fids_to_delete:
            resp = client.fs_delete(fids_to_delete)
            if resp.get('state'):
                logger.info(f"  💥 [联动删除] 成功在 115 网盘物理删除了 {len(fids_to_delete)} 个文件！")
            else:
                logger.error(f"  ❌ [联动删除] 115 删除接口调用失败: {resp}")

            # 5. 鞭尸检查：如果主目录里已经没有视频文件了，连目录一起扬了
            video_count = 0
            def count_videos(cid):
                nonlocal video_count
                res = client.fs_files({'cid': cid, 'limit': 1000})
                for item in res.get('data', []):
                    if item.get('fid'):
                        ext = str(item.get('n', '')).split('.')[-1].lower()
                        if ext in ['mp4', 'mkv', 'avi', 'ts', 'iso']:
                            video_count += 1
                    elif item.get('cid'):
                        count_videos(item.get('cid'))

            count_videos(base_cid)
            if video_count == 0:
                client.fs_delete(base_cid)
                P115CacheManager.delete_cid(base_cid) # 清理本地缓存
                logger.info(f"  🧹 [联动删除] 清理主目录缓存: {tmdb_folder_name}")
        else:
            logger.warning(f"  ⚠️ [联动删除] 扫描完毕，但未在网盘找到匹配的提取码文件。")

    except Exception as e:
        logger.error(f"  ❌ [联动删除] 执行异常: {e}", exc_info=True)