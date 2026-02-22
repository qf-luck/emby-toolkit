# reverse_proxy.py (最终完美版 V5 - 实时架构适配)

import logging
import requests
import re
import os
import json
from flask import Flask, request, Response
from urllib.parse import urlparse, urlunparse
from datetime import datetime, timedelta
import time
import uuid 
from flask import send_file 
from handler.poster_generator import get_missing_poster
from gevent import spawn, joinall
from websocket import create_connection
from database import custom_collection_db, queries_db
from database.connection import get_db_connection
from handler.custom_collection import RecommendationEngine
import config_manager
import constants
from routes.p115 import _get_cached_115_url

import extensions
import handler.emby as emby
logger = logging.getLogger(__name__)

MISSING_ID_PREFIX = "-800000_"

def to_missing_item_id(tmdb_id): 
    return f"{MISSING_ID_PREFIX}{tmdb_id}"

def is_missing_item_id(item_id):
    return isinstance(item_id, str) and item_id.startswith(MISSING_ID_PREFIX)

def parse_missing_item_id(item_id):
    # 从 -800000_12345 中提取出 12345
    return item_id.replace(MISSING_ID_PREFIX, "")
MIMICKED_ID_BASE = 900000
def to_mimicked_id(db_id): return str(-(MIMICKED_ID_BASE + db_id))
def from_mimicked_id(mimicked_id): return -(int(mimicked_id)) - MIMICKED_ID_BASE
def is_mimicked_id(item_id):
    try: return isinstance(item_id, str) and item_id.startswith('-')
    except: return False
MIMICKED_ITEMS_RE = re.compile(r'/emby/Users/([^/]+)/Items/(-(\d+))')
MIMICKED_ITEM_DETAILS_RE = re.compile(r'emby/Users/([^/]+)/Items/(-(\d+))$')

def _get_real_emby_url_and_key():
    base_url = config_manager.APP_CONFIG.get("emby_server_url", "").rstrip('/')
    api_key = config_manager.APP_CONFIG.get("emby_api_key", "")
    if not base_url or not api_key: raise ValueError("Emby服务器地址或API Key未配置")
    return base_url, api_key

def _fetch_items_in_chunks(base_url, api_key, user_id, item_ids, fields):
    """
    并发分块获取 Emby 项目详情。
    """
    if not item_ids: return []
    
    # 去重
    unique_ids = list(dict.fromkeys(item_ids))
    
    def chunk_list(lst, n):
        for i in range(0, len(lst), n): yield lst[i:i + n]
    
    # 适当增大分块大小以减少请求数
    id_chunks = list(chunk_list(unique_ids, 200))
    target_url = f"{base_url}/emby/Users/{user_id}/Items"
    
    def fetch_chunk(chunk):
        params = {'api_key': api_key, 'Ids': ",".join(chunk), 'Fields': fields}
        try:
            resp = requests.get(target_url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json().get("Items", [])
        except Exception as e:
            logger.error(f"并发获取某分块数据时失败: {e}")
            return None
            
    greenlets = [spawn(fetch_chunk, chunk) for chunk in id_chunks]
    joinall(greenlets)
    
    all_items = []
    for g in greenlets:
        if g.value: all_items.extend(g.value)
        
    return all_items

def _fetch_sorted_items_via_emby_proxy(user_id, item_ids, sort_by, sort_order, limit, offset, fields, total_record_count):
    """
    [榜单类专用] 
    当我们需要对一组固定的 ID (来自榜单) 进行排序和分页时使用。
    利用 Emby 的 GET 请求能力，让 Emby 帮我们过滤权限并排序。
    如果 ID 太多，回退到内存排序。
    """
    base_url, api_key = _get_real_emby_url_and_key()
    
    # 估算 URL 长度
    estimated_ids_length = len(item_ids) * 33 # GUID 长度 + 逗号
    URL_LENGTH_THRESHOLD = 1800 # 保守阈值

    try:
        if estimated_ids_length < URL_LENGTH_THRESHOLD:
            # --- 路径 A: ID列表较短，直接请求 Emby (最快，且自动处理权限) ---
            logger.trace(f"  ➜ [Emby 代理排序] ID列表较短 ({len(item_ids)}个)，使用 GET 方法。")
            target_url = f"{base_url}/emby/Users/{user_id}/Items"
            emby_params = {
                'api_key': api_key, 'Ids': ",".join(item_ids), 'Fields': fields,
                'SortBy': sort_by, 'SortOrder': sort_order,
                'StartIndex': offset, 'Limit': limit,
            }
            resp = requests.get(target_url, params=emby_params, timeout=25)
            resp.raise_for_status()
            emby_data = resp.json()
            # 注意：Emby 返回的 TotalRecordCount 是经过权限过滤后的数量
            # 如果我们传入的 total_record_count 是全量的，这里可能需要修正，但为了分页条正常，通常直接用 Emby 返回的
            return emby_data
        else:
            # --- 路径 B: ID列表超长，内存排序 (安全回退) ---
            logger.trace(f"  ➜ [内存排序回退] ID列表超长 ({len(item_ids)}个)，启动内存排序。")
            
            # 1. 获取所有项目的详情 (Emby 会自动过滤掉无权访问的项目)
            # 我们需要获取用于排序的字段
            primary_sort_by = sort_by.split(',')[0]
            fields_for_sorting = f"{fields},{primary_sort_by}"
            
            all_items_details = _fetch_items_in_chunks(base_url, api_key, user_id, item_ids, fields_for_sorting)
            
            # 更新总数 (过滤后的真实数量)
            real_total_count = len(all_items_details)

            # 2. 在内存中排序
            try:
                is_desc = sort_order == 'Descending'
                
                def get_sort_val(item):
                    val = item.get(primary_sort_by)
                    # 处理日期
                    if 'Date' in primary_sort_by or 'Year' in primary_sort_by:
                        return val or "1900-01-01T00:00:00.000Z"
                    # 处理数字
                    if 'Rating' in primary_sort_by or 'Count' in primary_sort_by:
                        return float(val) if val is not None else 0
                    # 处理字符串
                    return str(val or "").lower()

                all_items_details.sort(key=get_sort_val, reverse=is_desc)
            except Exception as sort_e:
                logger.error(f"  ➜ 内存排序时发生错误: {sort_e}", exc_info=True)
            
            # 3. 在内存中分页
            paginated_items = all_items_details[offset : offset + limit]
            
            return {"Items": paginated_items, "TotalRecordCount": real_total_count}

    except Exception as e:
        logger.error(f"  ➜ Emby代理排序或内存回退时失败: {e}", exc_info=True)
        return {"Items": [], "TotalRecordCount": 0}

def handle_get_views():
    """
    获取用户的主页视图列表。
    """
    real_server_id = extensions.EMBY_SERVER_ID
    if not real_server_id:
        return "Proxy is not ready", 503

    try:
        user_id_match = re.search(r'/emby/Users/([^/]+)/Views', request.path)
        if not user_id_match:
            return "Could not determine user from request path", 400
        user_id = user_id_match.group(1)

        # 1. 获取原生库
        user_visible_native_libs = emby.get_emby_libraries(
            config_manager.APP_CONFIG.get("emby_server_url", ""),
            config_manager.APP_CONFIG.get("emby_api_key", ""),
            user_id
        )
        if user_visible_native_libs is None: user_visible_native_libs = []

        # 2. 生成虚拟库
        collections = custom_collection_db.get_all_active_custom_collections()
        fake_views_items = []
        
        for coll in collections:
            # 物理检查：库在Emby里有实体吗？
            real_emby_collection_id = coll.get('emby_collection_id')
            if not real_emby_collection_id:
                continue

            # 权限检查：如果设置了 allowed_user_ids，则检查
            allowed_users = coll.get('allowed_user_ids')
            if allowed_users and isinstance(allowed_users, list):
                if user_id not in allowed_users:
                    continue
            
            # 生成虚拟库对象
            db_id = coll['id']
            mimicked_id = to_mimicked_id(db_id)
            # 使用时间戳强制刷新封面
            image_tags = {"Primary": f"{real_emby_collection_id}?timestamp={int(time.time())}"}
            definition = coll.get('definition_json') or {}
            
            item_type_from_db = definition.get('item_type', 'Movie')
            collection_type = "mixed"
            if not (isinstance(item_type_from_db, list) and len(item_type_from_db) > 1):
                 authoritative_type = item_type_from_db[0] if isinstance(item_type_from_db, list) and item_type_from_db else item_type_from_db if isinstance(item_type_from_db, str) else 'Movie'
                 collection_type = "tvshows" if authoritative_type == 'Series' else "movies"

            fake_view = {
                "Name": coll['name'], "ServerId": real_server_id, "Id": mimicked_id,
                "Guid": str(uuid.uuid4()), "Etag": f"{db_id}{int(time.time())}",
                "DateCreated": "2025-01-01T00:00:00.0000000Z", "CanDelete": False, "CanDownload": False,
                "SortName": coll['name'], "ExternalUrls": [], "ProviderIds": {}, "IsFolder": True,
                "ParentId": "2", "Type": "CollectionFolder", "PresentationUniqueKey": str(uuid.uuid4()),
                "DisplayPreferencesId": f"custom-{db_id}", "ForcedSortName": coll['name'],
                "Taglines": [], "RemoteTrailers": [],
                "UserData": {"PlaybackPositionTicks": 0, "IsFavorite": False, "Played": False},
                "ChildCount": coll.get('in_library_count', 1),
                "PrimaryImageAspectRatio": 1.7777777777777777, 
                "CollectionType": collection_type, "ImageTags": image_tags, "BackdropImageTags": [], 
                "LockedFields": [], "LockData": False
            }
            fake_views_items.append(fake_view)
        
        # 3. 合并与排序
        native_views_items = []
        should_merge_native = config_manager.APP_CONFIG.get('proxy_merge_native_libraries', True)
        if should_merge_native:
            all_native_views = user_visible_native_libs
            raw_selection = config_manager.APP_CONFIG.get('proxy_native_view_selection', '')
            selected_native_view_ids = [x.strip() for x in raw_selection.split(',') if x.strip()] if isinstance(raw_selection, str) else raw_selection
            
            if selected_native_view_ids:
                native_views_items = [view for view in all_native_views if view.get("Id") in selected_native_view_ids]
            else:
                native_views_items = []
        
        final_items = []
        native_order = config_manager.APP_CONFIG.get('proxy_native_view_order', 'before')
        if native_order == 'after':
            final_items.extend(fake_views_items)
            final_items.extend(native_views_items)
        else:
            final_items.extend(native_views_items)
            final_items.extend(fake_views_items)

        final_response = {"Items": final_items, "TotalRecordCount": len(final_items)}
        return Response(json.dumps(final_response), mimetype='application/json')
        
    except Exception as e:
        logger.error(f"[PROXY] 获取视图数据时出错: {e}", exc_info=True)
        return "Internal Proxy Error", 500

def handle_get_mimicked_library_details(user_id, mimicked_id):
    try:
        real_db_id = from_mimicked_id(mimicked_id)
        coll = custom_collection_db.get_custom_collection_by_id(real_db_id)
        if not coll: return "Not Found", 404

        real_server_id = extensions.EMBY_SERVER_ID
        real_emby_collection_id = coll.get('emby_collection_id')
        image_tags = {"Primary": real_emby_collection_id} if real_emby_collection_id else {}
        
        definition = coll.get('definition_json') or {}
        item_type_from_db = definition.get('item_type', 'Movie')
        collection_type = "mixed"
        if not (isinstance(item_type_from_db, list) and len(item_type_from_db) > 1):
             authoritative_type = item_type_from_db[0] if isinstance(item_type_from_db, list) and item_type_from_db else item_type_from_db if isinstance(item_type_from_db, str) else 'Movie'
             collection_type = "tvshows" if authoritative_type == 'Series' else "movies"

        fake_library_details = {
            "Name": coll['name'], "ServerId": real_server_id, "Id": mimicked_id,
            "Type": "CollectionFolder",
            "CollectionType": collection_type, "IsFolder": True, "ImageTags": image_tags,
        }
        return Response(json.dumps(fake_library_details), mimetype='application/json')
    except Exception as e:
        logger.error(f"获取伪造库详情时出错: {e}", exc_info=True)
        return "Internal Server Error", 500

def handle_get_mimicked_library_image(path):
    try:
        tag_with_timestamp = request.args.get('tag') or request.args.get('Tag')
        if not tag_with_timestamp: return "Bad Request", 400
        real_emby_collection_id = tag_with_timestamp.split('?')[0]
        base_url, _ = _get_real_emby_url_and_key()
        image_url = f"{base_url}/Items/{real_emby_collection_id}/Images/Primary"
        headers = {key: value for key, value in request.headers if key.lower() != 'host'}
        headers['Host'] = urlparse(base_url).netloc
        resp = requests.get(image_url, headers=headers, stream=True, params=request.args)
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        response_headers = [(name, value) for name, value in resp.raw.headers.items() if name.lower() not in excluded_headers]
        return Response(resp.iter_content(chunk_size=8192), resp.status_code, response_headers)
    except Exception as e:
        return "Internal Proxy Error", 500

UNSUPPORTED_METADATA_ENDPOINTS = [
        # '/Items/Prefixes', # Emby 不支持按前缀过滤虚拟库
        '/Genres',         
        '/Studios',        
        '/Tags',           
        '/OfficialRatings',
        '/Years'           
    ]

def handle_mimicked_library_metadata_endpoint(path, mimicked_id, params):
    """
    处理虚拟库的元数据请求。
    """
    if any(path.endswith(endpoint) for endpoint in UNSUPPORTED_METADATA_ENDPOINTS):
        return Response(json.dumps([]), mimetype='application/json')

    try:
        real_db_id = from_mimicked_id(mimicked_id)
        collection_info = custom_collection_db.get_custom_collection_by_id(real_db_id)
        if not collection_info or not collection_info.get('emby_collection_id'):
            return Response(json.dumps([]), mimetype='application/json')

        real_emby_collection_id = collection_info.get('emby_collection_id')
        base_url, api_key = _get_real_emby_url_and_key()
        target_url = f"{base_url}/{path}"
        
        headers = {k: v for k, v in request.headers if k.lower() not in ['host']}
        headers['Host'] = urlparse(base_url).netloc
        
        new_params = params.copy()
        new_params['ParentId'] = real_emby_collection_id
        new_params['api_key'] = api_key
        
        resp = requests.get(target_url, headers=headers, params=new_params, timeout=15)
        resp.raise_for_status()
        
        return Response(resp.content, resp.status_code, content_type=resp.headers.get('Content-Type'))

    except Exception as e:
        logger.error(f"处理虚拟库元数据请求 '{path}' 时出错: {e}", exc_info=True)
        return Response(json.dumps([]), mimetype='application/json')
    
def handle_get_mimicked_library_items(user_id, mimicked_id, params):
    """
    【V8 - 实时架构 + 占位海报适配版 + 排序修复】
    支持：实时权限过滤、原生排序、榜单占位符、数量限制
    """
    try:
        # 1. 获取合集基础信息
        real_db_id = from_mimicked_id(mimicked_id)
        collection_info = custom_collection_db.get_custom_collection_by_id(real_db_id)
        if not collection_info:
            return Response(json.dumps({"Items": [], "TotalRecordCount": 0}), mimetype='application/json')

        definition = collection_info.get('definition_json') or {}
        if isinstance(definition, str):
            try: definition = json.loads(definition)
            except: definition = {}

        collection_type = collection_info.get('type')
        
        # 2. 获取分页和排序参数 (变量定义必须在此处)
        emby_limit = int(params.get('Limit', 50))
        offset = int(params.get('StartIndex', 0))
        
        defined_limit = definition.get('limit')
        if defined_limit:
            defined_limit = int(defined_limit)
        
        # --- 排序优先级逻辑 ---
        req_sort_by = params.get('SortBy')
        req_sort_order = params.get('SortOrder')
        
        defined_sort_by = definition.get('default_sort_by')
        defined_sort_order = definition.get('default_sort_order')

        # 逻辑：如果DB定义了且不是none，强制劫持；否则使用客户端请求
        if defined_sort_by and defined_sort_by != 'none':
            # 强制劫持模式
            sort_by = defined_sort_by
            sort_order = defined_sort_order or 'Descending'
            is_native_mode = False
        else:
            # 原生/客户端模式 (设置为 NONE 时)
            sort_by = req_sort_by or 'DateCreated'
            sort_order = req_sort_order or 'Descending'
            is_native_mode = True

        # 核心判断：是否需要 Emby 原生排序
        # 当使用原生排序(is_native_mode=True)时，如果排序字段不是数据库能完美处理的(如DateCreated)，
        # 必须强制走 Emby 代理排序。
        is_emby_proxy_sort_required = (
            collection_type in ['ai_recommendation', 'ai_recommendation_global'] or 
            'DateLastContentAdded' in sort_by or
            (is_native_mode and sort_by not in ['DateCreated', 'Random'])
        )

        # 3. 准备基础查询参数
        tmdb_ids_filter = None
        rules = definition.get('rules', [])
        logic = definition.get('logic', 'AND')
        item_types = definition.get('item_type', ['Movie'])
        target_library_ids = definition.get('target_library_ids', [])

        # 4. 分流处理逻辑
        
        # --- 场景 A: 榜单类 (需要处理占位符 + 严格权限过滤) ---
        if collection_type == 'list':
            show_placeholders = config_manager.APP_CONFIG.get(constants.CONFIG_OPTION_PROXY_SHOW_MISSING_PLACEHOLDERS, False)
            raw_list_json = collection_info.get('generated_media_info_json')
            raw_list = json.loads(raw_list_json) if isinstance(raw_list_json, str) else (raw_list_json or [])
            
            if raw_list:
                # 1. 获取该榜单中所有涉及的 TMDb ID
                tmdb_ids_in_list = [str(i.get('tmdb_id')) for i in raw_list if i.get('tmdb_id')]
                
                # 2. 【用户视图】获取当前用户有权看到的项目
                items_in_db, _ = queries_db.query_virtual_library_items(
                    rules=rules, logic=logic, user_id=user_id,
                    limit=2000, offset=0, 
                    sort_by='DateCreated', sort_order='Descending',
                    item_types=item_types, target_library_ids=target_library_ids,
                    tmdb_ids=tmdb_ids_in_list
                )
                
                # 3. 【全局视图】获取Emby中实际存在的项目（忽略用户权限，传入 user_id=None）
                global_existing_items, _ = queries_db.query_virtual_library_items(
                    rules=rules, logic=logic, user_id=None, 
                    limit=2000, offset=0,
                    item_types=item_types, target_library_ids=target_library_ids,
                    tmdb_ids=tmdb_ids_in_list
                )

                # 4. 建立映射表
                local_tmdb_map = {str(i['tmdb_id']): i['Id'] for i in items_in_db if i.get('tmdb_id')}
                local_emby_id_set = {str(i['Id']) for i in items_in_db}
                
                global_tmdb_set = {str(i['tmdb_id']) for i in global_existing_items if i.get('tmdb_id')}
                global_emby_id_set = {str(i['Id']) for i in global_existing_items}
                
                # 5. 构造完整视图列表
                full_view_list = []
                for raw_item in raw_list:
                    tid = str(raw_item.get('tmdb_id')) if raw_item.get('tmdb_id') else "None"
                    eid = str(raw_item.get('emby_id')) if raw_item.get('emby_id') else "None"

                    if (not tid or tid.lower() == "none") and (not eid or eid.lower() == "none"):
                        continue

                    if defined_limit and len(full_view_list) >= defined_limit:
                        break
                    
                    # 分支 1: 用户有权查看
                    if tid != "None" and tid in local_tmdb_map:
                        full_view_list.append({"is_missing": False, "id": local_tmdb_map[tid], "tmdb_id": tid})
                    elif eid != "None" and eid in local_emby_id_set:
                         full_view_list.append({"is_missing": False, "id": eid, "tmdb_id": tid})

                    # 分支 3: 项目存在于全局库，但用户无权查看 -> 【跳过，不显示占位符】
                    elif (tid != "None" and tid in global_tmdb_set) or (eid != "None" and eid in global_emby_id_set):
                        continue 

                    # 分支 4: 项目确实缺失 -> 显示占位符
                    elif tid != "None":
                        if show_placeholders:
                            full_view_list.append({"is_missing": True, "tmdb_id": tid})

                # 6. 分页
                paged_part = full_view_list[offset : offset + emby_limit]
                reported_total_count = len(full_view_list)

                # 7. 批量获取详情
                real_eids = [x['id'] for x in paged_part if not x['is_missing']]
                missing_tids = [x['tmdb_id'] for x in paged_part if x['is_missing']]
                
                status_map = queries_db.get_missing_items_metadata(missing_tids)
                
                base_url, api_key = _get_real_emby_url_and_key()
                full_fields = "PrimaryImageAspectRatio,ImageTags,HasPrimaryImage,ProviderIds,UserData,Name,ProductionYear,CommunityRating,Type"
                emby_details = _fetch_items_in_chunks(base_url, api_key, user_id, real_eids, full_fields)
                emby_map = {item['Id']: item for item in emby_details}

                final_items = []
                for entry in paged_part:
                    if not entry['is_missing']:
                        eid = entry['id']
                        if eid in emby_map:
                            final_items.append(emby_map[eid])
                    else:
                        # 占位符构造逻辑
                        tid = entry['tmdb_id']
                        meta = status_map.get(tid, {})
                        status = meta.get('subscription_status', 'WANTED')
                        db_item_type = meta.get('item_type', 'Movie')
                        
                        placeholder = {
                            "Name": meta.get('title', '未知内容'),
                            "ServerId": extensions.EMBY_SERVER_ID,
                            "Id": to_missing_item_id(tid),
                            "Type": db_item_type,
                            "ProductionYear": int(meta.get('release_year')) if meta.get('release_year') else None,
                            "ImageTags": {"Primary": f"missing_{status}_{tid}"},
                            "HasPrimaryImage": True,
                            "PrimaryImageAspectRatio": 0.6666666666666666,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False},
                            "ProviderIds": {"Tmdb": tid},
                            "LocationType": "Virtual"
                        }
                        r_date = meta.get('release_date')
                        r_year = meta.get('release_year')
                        if r_date:
                            try:
                                if hasattr(r_date, 'strftime'):
                                    placeholder["PremiereDate"] = r_date.strftime('%Y-%m-%dT00:00:00.0000000Z')
                                else:
                                    placeholder["PremiereDate"] = str(r_date)
                            except: pass
                        if "PremiereDate" not in placeholder and r_year:
                            placeholder["PremiereDate"] = f"{r_year}-01-01T00:00:00.0000000Z"
                        if db_item_type == 'Series':
                            placeholder["Status"] = "Released"

                        final_items.append(placeholder)
                
                return Response(json.dumps({"Items": final_items, "TotalRecordCount": reported_total_count}), mimetype='application/json')

        # --- 场景 B: 筛选/推荐类 (修复灰色占位符) ---
        else:
            if collection_type in ['ai_recommendation', 'ai_recommendation_global']:
                api_key = config_manager.APP_CONFIG.get("tmdb_api_key")
                if api_key:
                    engine = RecommendationEngine(api_key)
                    if collection_type == 'ai_recommendation':
                        candidate_pool = engine.generate_user_vector(user_id, limit=300, allowed_types=item_types)
                    else:
                        candidate_pool = engine.generate_global_vector(limit=300, allowed_types=item_types)
                    tmdb_ids_filter = [str(i['id']) for i in candidate_pool]

            # 执行 SQL 查询
            sql_limit = defined_limit if is_emby_proxy_sort_required and defined_limit else 5000 if is_emby_proxy_sort_required else min(emby_limit, defined_limit - offset) if (defined_limit and defined_limit > offset) else emby_limit
            sql_offset = 0 if is_emby_proxy_sort_required else offset
            sql_sort = 'Random' if 'ai_recommendation' in collection_type else sort_by

            items, total_count = queries_db.query_virtual_library_items(
                rules=rules, logic=logic, user_id=user_id,
                limit=sql_limit, offset=sql_offset,
                sort_by=sql_sort, sort_order=sort_order,
                item_types=item_types, target_library_ids=target_library_ids,
                tmdb_ids=tmdb_ids_filter
            )

            reported_total_count = min(total_count, defined_limit) if defined_limit else total_count

            if not items:
                return Response(json.dumps({"Items": [], "TotalRecordCount": reported_total_count}), mimetype='application/json')

            final_emby_ids = [i['Id'] for i in items]
            full_fields = "PrimaryImageAspectRatio,ImageTags,HasPrimaryImage,ProviderIds,UserData,Name,ProductionYear,CommunityRating,DateCreated,PremiereDate,Type,RecursiveItemCount,SortName,ChildCount,BasicSyncInfo"

            if is_emby_proxy_sort_required:
                # 代理排序模式：将所有 ID 交给 Emby (或内存) 进行排序和分页
                sorted_data = _fetch_sorted_items_via_emby_proxy(
                    user_id, final_emby_ids, sort_by, sort_order, emby_limit, offset, full_fields, reported_total_count
                )
                return Response(json.dumps(sorted_data), mimetype='application/json')
            else:
                # SQL 排序模式：直接获取详情
                base_url, api_key = _get_real_emby_url_and_key()
                items_from_emby = _fetch_items_in_chunks(base_url, api_key, user_id, final_emby_ids, full_fields)
                items_map = {item['Id']: item for item in items_from_emby}
                
                # 过滤掉 Emby 实际没有返回的项目
                final_items = [items_map[eid] for eid in final_emby_ids if eid in items_map]
                
                # --- 修复开始 ---
                expected_count = len(final_emby_ids)
                actual_count = len(final_items)
                
                if actual_count < expected_count:
                    diff = expected_count - actual_count
                    # 1. 先执行原本的减法修正
                    reported_total_count = max(0, reported_total_count - diff)
                    logger.debug(f"检测到权限过滤导致的数量差异: SQL={expected_count}, Emby={actual_count}. 初步修正 TotalRecordCount 为 {reported_total_count}")

                    # 2. 【新增】封底保险逻辑
                    if reported_total_count <= emby_limit:
                        reported_total_count = actual_count
                        logger.debug(f"修正后的总数小于分页限制，强制对齐 TotalRecordCount = {actual_count} 以消除灰块")

                return Response(json.dumps({"Items": final_items, "TotalRecordCount": reported_total_count}), mimetype='application/json')

    except Exception as e:
        logger.error(f"处理虚拟库 '{mimicked_id}' 失败: {e}", exc_info=True)
        return Response(json.dumps({"Items": [], "TotalRecordCount": 0}), mimetype='application/json')

def handle_get_latest_items(user_id, params):
    """
    获取最新项目。
    利用 queries_db 的排序能力，快速返回结果。
    【修复版】增加对榜单(list)和AI合集的类型判断，防止无规则合集泄露全局最新数据。
    """
    try:
        base_url, api_key = _get_real_emby_url_and_key()
        virtual_library_id = params.get('ParentId') or params.get('customViewId')
        limit = int(params.get('Limit', 20))
        fields = params.get('Fields', "PrimaryImageAspectRatio,BasicSyncInfo,DateCreated,UserData")

        # --- 辅助函数：获取合集的过滤 ID ---
        def get_collection_filter_ids(coll_data):
            c_type = coll_data.get('type')
            # 1. 榜单类：必须限制在榜单包含的 TMDb ID 范围内
            if c_type == 'list':
                raw_json = coll_data.get('generated_media_info_json')
                raw_list = json.loads(raw_json) if isinstance(raw_json, str) else (raw_json or [])
                return [str(i.get('tmdb_id')) for i in raw_list if i.get('tmdb_id')]
            # 2. AI 推荐类：暂不支持“最新”视图 (因为是动态生成的)，返回一个不存在的 ID 防止泄露
            elif c_type in ['ai_recommendation', 'ai_recommendation_global']:
                return ["-1"] 
            # 3. 规则类：返回 None，表示不限制 ID，只走 Rules
            return None

        # 场景一：单个虚拟库的最新
        if virtual_library_id and is_mimicked_id(virtual_library_id):
            real_db_id = from_mimicked_id(virtual_library_id)
            collection_info = custom_collection_db.get_custom_collection_by_id(real_db_id)
            if not collection_info: return Response(json.dumps([]), mimetype='application/json')

            definition = collection_info.get('definition_json') or {}
            if isinstance(definition, str): definition = json.loads(definition)
            
            if not definition.get('show_in_latest', True):
                return Response(json.dumps([]), mimetype='application/json')

            # --- 修复核心：获取 ID 过滤器 ---
            tmdb_ids_filter = get_collection_filter_ids(collection_info)
            # 如果是 AI 合集返回了 ["-1"]，或者榜单为空，直接返回空结果
            if tmdb_ids_filter is not None and (len(tmdb_ids_filter) == 0 or tmdb_ids_filter == ["-1"]):
                 return Response(json.dumps([]), mimetype='application/json')

            # 确定排序
            item_types = definition.get('item_type', ['Movie'])
            is_series_only = isinstance(item_types, list) and len(item_types) == 1 and item_types[0] == 'Series'
            sort_by = 'DateLastContentAdded,DateCreated' if is_series_only else 'DateCreated'

            # SQL 过滤权限和规则
            items, total_count = queries_db.query_virtual_library_items(
                rules=definition.get('rules', []), logic=definition.get('logic', 'AND'),
                user_id=user_id, limit=500, offset=0,
                sort_by='DateCreated', sort_order='Descending',
                item_types=item_types, target_library_ids=definition.get('target_library_ids', []),
                tmdb_ids=tmdb_ids_filter  # <--- 传入 TMDb ID 限制
            )
            
            if not items: return Response(json.dumps([]), mimetype='application/json')
            final_emby_ids = [i['Id'] for i in items]

            # 统一调用代理排序
            sorted_data = _fetch_sorted_items_via_emby_proxy(
                user_id, final_emby_ids, sort_by, 'Descending', limit, 0, fields, len(final_emby_ids)
            )
            return Response(json.dumps(sorted_data.get("Items", [])), mimetype='application/json')

        # 场景二：全局最新 (所有可见合集的聚合)
        elif not virtual_library_id:
            # 获取所有开启了“显示最新”的合集 ID
            included_collection_ids = custom_collection_db.get_active_collection_ids_for_latest_view()
            if not included_collection_ids:
                return Response(json.dumps([]), mimetype='application/json')
            
            all_latest = []
            for coll_id in included_collection_ids:
                coll = custom_collection_db.get_custom_collection_by_id(coll_id)
                if not coll: continue
                
                # 检查权限
                allowed_users = coll.get('allowed_user_ids')
                if allowed_users and user_id not in allowed_users: continue

                # --- 修复核心：获取 ID 过滤器 ---
                tmdb_ids_filter = get_collection_filter_ids(coll)
                if tmdb_ids_filter is not None and (len(tmdb_ids_filter) == 0 or tmdb_ids_filter == ["-1"]):
                    continue

                definition = coll.get('definition_json')
                items, _ = queries_db.query_virtual_library_items(
                    rules=definition.get('rules', []),
                    logic=definition.get('logic', 'AND'),
                    user_id=user_id,
                    limit=limit, 
                    offset=0,
                    sort_by='DateCreated',
                    sort_order='Descending',
                    item_types=definition.get('item_type', ['Movie']),
                    target_library_ids=definition.get('target_library_ids', []),
                    tmdb_ids=tmdb_ids_filter # <--- 传入 TMDb ID 限制
                )
                all_latest.extend(items)
            
            # 去重并获取详情
            unique_ids = list({i['Id'] for i in all_latest})
            if not unique_ids: return Response(json.dumps([]), mimetype='application/json')
            
            # 批量获取详情
            items_details = _fetch_items_in_chunks(base_url, api_key, user_id, unique_ids, "DateCreated")
            # 内存排序
            items_details.sort(key=lambda x: x.get('DateCreated', ''), reverse=True)
            # 截取
            latest_ids = [i['Id'] for i in items_details[:limit]]

        else:
            # 原生库请求，直接转发
            target_url = f"{base_url}/{request.path.lstrip('/')}"
            forward_headers = {k: v for k, v in request.headers if k.lower() not in ['host', 'accept-encoding']}
            forward_headers['Host'] = urlparse(base_url).netloc
            forward_params = request.args.copy()
            forward_params['api_key'] = api_key
            resp = requests.request(method=request.method, url=target_url, headers=forward_headers, params=forward_params, data=request.get_data(), stream=True, timeout=30.0)
            excluded_resp_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
            response_headers = [(name, value) for name, value in resp.raw.headers.items() if name.lower() not in excluded_resp_headers]
            return Response(resp.iter_content(chunk_size=8192), resp.status_code, response_headers)

        if not latest_ids:
            return Response(json.dumps([]), mimetype='application/json')

        # 获取最终详情
        items_from_emby = _fetch_items_in_chunks(base_url, api_key, user_id, latest_ids, fields)
        items_map = {item['Id']: item for item in items_from_emby}
        final_items = [items_map[id] for id in latest_ids if id in items_map]
        
        return Response(json.dumps(final_items), mimetype='application/json')

    except Exception as e:
        logger.error(f"  ➜ 处理最新媒体时发生未知错误: {e}", exc_info=True)
        return Response(json.dumps([]), mimetype='application/json')

proxy_app = Flask(__name__)

@proxy_app.route('/', defaults={'path': ''})
@proxy_app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS'])
def proxy_all(path):
    # --- 1. WebSocket 代理逻辑 ---
    if 'Upgrade' in request.headers and request.headers.get('Upgrade', '').lower() == 'websocket':
        ws_client = request.environ.get('wsgi.websocket')
        if not ws_client: return "WebSocket upgrade failed", 400

        try:
            base_url, _ = _get_real_emby_url_and_key()
            parsed_url = urlparse(base_url)
            ws_scheme = 'wss' if parsed_url.scheme == 'https' else 'ws'
            target_ws_url = urlunparse((ws_scheme, parsed_url.netloc, f'/{path}', '', request.query_string.decode(), ''))
            
            headers_to_server = {k: v for k, v in request.headers.items() if k.lower() not in ['host', 'upgrade', 'connection', 'sec-websocket-key', 'sec-websocket-version']}
            ws_server = create_connection(target_ws_url, header=headers_to_server, timeout=10)

            def forward_to_server():
                try:
                    while not ws_client.closed and ws_server.connected:
                        message = ws_client.receive()
                        if message is not None: ws_server.send(message)
                        else: break
                except: pass
                finally: ws_server.close()

            def forward_to_client():
                try:
                    while ws_server.connected and not ws_client.closed:
                        message = ws_server.recv()
                        if message is not None: ws_client.send(message)
                        else: break
                except: pass
                finally: ws_client.close()
            
            greenlets = [spawn(forward_to_server), spawn(forward_to_client)]
            joinall(greenlets)
            
            # WebSocket 结束后返回空响应
            return Response()

        except Exception as e:
            logger.error(f"WebSocket 代理错误: {e}")
            return Response(status=500)

    # --- 2. HTTP 代理逻辑 ---
    try:
        full_path = f'/{path}'

        # ====================================================================
        # ★★★ 终极拦截 A+：全盘接管视频流 302 直链解析 (复刻 CMS 核心逻辑) ★★★
        # 当客户端请求视频流时，反代层主动查询文件路径并剥离 115 直链！
        # ====================================================================
        if '/videos/' in full_path and ('/stream' in full_path or '/original' in full_path or 'PlaybackInfo' in full_path):
            try:
                # 1. 抓取请求流的项目 ID
                item_id_match = re.search(r'/Items/([^/]+)/', full_path) or re.search(r'/videos/([^/]+)/', full_path)
                if item_id_match:
                    item_id = item_id_match.group(1)
                    base_url, api_key = _get_real_emby_url_and_key()
                    user_id = request.args.get('UserId') or request.args.get('api_key') or "admin"
                    
                    # 2. 向局域网内的 Emby 打听这个视频的实际物理路径
                    details_url = f"{base_url}/emby/Items/{item_id}"
                    resp = requests.get(details_url, params={'api_key': api_key, 'UserId': user_id}, timeout=3)
                    
                    if resp.status_code == 200:
                        item_data = resp.json()
                        file_path = item_data.get('Path', '')
                        
                        # 3. 核心判断：是 .strm 文件吗？本地能读到吗？
                        if file_path and file_path.endswith('.strm') and os.path.exists(file_path):
                            with open(file_path, 'r', encoding='utf-8') as f:
                                strm_content = f.read().strip()
                                
                            # 4. 从局域网链接中提取提取码 (pick_code)
                            # strm 格式: http://192.168.X.X:5257/api/p115/play/abc1234
                            if '/api/p115/play/' in strm_content:
                                pick_code = strm_content.split('/play/')[-1].split('?')[0].strip()
                                
                                # 5. ★ 决战 115：获取直链并直接返回 302！
                                # 注意：必须使用当前发起请求的客户端的 User-Agent，否则 115 CDN 报 403
                                player_ua = request.headers.get('User-Agent', 'Mozilla/5.0')
                                client_ip = request.headers.get('X-Real-IP', request.remote_addr)
                                
                                # 调用内存缓存版的直链获取器
                                real_url = _get_cached_115_url(pick_code, player_ua, client_ip)
                                
                                if real_url:
                                    logger.info(f"  🎬 [反代劫持] 成功拦截 Emby 流请求，下发 115 CDN 直链！")
                                    from flask import redirect
                                    
                                    # 如果是 PlaybackInfo 请求，返回给客户端的 Path 应该是原始的 strm 地址或者再次经过我们代理的地址
                                    if 'PlaybackInfo' in full_path:
                                        fake_info = {
                                            "MediaSources": [{
                                                "Id": item_id,
                                                "Path": real_url,
                                                "Protocol": "Http",
                                                "IsInfiniteStream": False,
                                                "SupportsDirectPlay": True,
                                                "SupportsDirectStream": True,
                                                "SupportsTranscoding": False,
                                                "Container": "mp4", # 显式告知容器格式
                                                "ReadAtNativeFramerate": False,
                                                "Type": "Default"
                                            }],
                                            "PlaySessionId": f"etk_proxy_{int(time.time())}"
                                        }
                                        # 强制指定内容长度和编码，防止某些客户端解析 500
                                        json_data = json.dumps(fake_info)
                                        return Response(
                                            json_data, 
                                            mimetype='application/json',
                                            headers={'Content-Length': str(len(json_data))}
                                        )
                                    
                                    # 真正的视频流请求，直接 302 甩出去
                                    return redirect(real_url, code=302)
            except Exception as e:
                logger.error(f"  ❌ 反代拦截解析直链出错，回退原生处理: {e}")

        # --- 拦截 A: 虚拟项目海报图片 ---
        if path.startswith('emby/Items/') and '/Images/Primary' in path:
            item_id = path.split('/')[2]
            if is_missing_item_id(item_id):
                combined_id = parse_missing_item_id(item_id)
                real_tmdb_id = combined_id.split('_S_')[0] if '_S_' in combined_id else combined_id
                meta = queries_db.get_best_metadata_by_tmdb_id(real_tmdb_id)
                db_status = meta.get('subscription_status', 'WANTED')
                current_status = db_status if db_status in ['WANTED', 'SUBSCRIBED', 'PENDING_RELEASE', 'PAUSED', 'IGNORED'] else 'WANTED'
                
                from handler.poster_generator import get_missing_poster
                img_file_path = get_missing_poster(
                    tmdb_id=real_tmdb_id, 
                    status=current_status,
                    poster_path=meta.get('poster_path')
                )
                
                if img_file_path and os.path.exists(img_file_path):
                    resp = send_file(img_file_path, mimetype='image/jpeg')
                    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                    return resp

        # --- 拦截 B: 视图列表 (Views) ---
        if path.endswith('/Views') and path.startswith('emby/Users/'):
            return handle_get_views()

        # --- 拦截 C: 最新项目 (Latest) ---
        if path.endswith('/Items/Latest'):
            user_id_match = re.search(r'/emby/Users/([^/]+)/', full_path)
            if user_id_match:
                return handle_get_latest_items(user_id_match.group(1), request.args)

        # --- 拦截 D: 虚拟库详情 ---
        details_match = MIMICKED_ITEM_DETAILS_RE.search(full_path)
        if details_match:
            user_id = details_match.group(1)
            mimicked_id = details_match.group(2)
            return handle_get_mimicked_library_details(user_id, mimicked_id)

        # --- 拦截 E: 虚拟库图片 ---
        if path.startswith('emby/Items/') and '/Images/' in path:
            item_id = path.split('/')[2]
            if is_mimicked_id(item_id):
                return handle_get_mimicked_library_image(path)
        
        # --- 拦截 F: 虚拟库内容浏览 (Items) ---
        parent_id = request.args.get("ParentId")
        if parent_id and is_mimicked_id(parent_id):
            # 处理元数据请求
            if any(path.endswith(endpoint) for endpoint in UNSUPPORTED_METADATA_ENDPOINTS + ['/Items/Prefixes', '/Genres', '/Studios', '/Tags', '/OfficialRatings', '/Years']):
                return handle_mimicked_library_metadata_endpoint(path, parent_id, request.args)
            
            # 处理内容列表请求
            user_id_match = re.search(r'emby/Users/([^/]+)/Items', path)
            if user_id_match:
                user_id = user_id_match.group(1)
                return handle_get_mimicked_library_items(user_id, parent_id, request.args)

        # 兜底逻辑
        base_url, api_key = _get_real_emby_url_and_key()
        target_url = f"{base_url}/{path.lstrip('/')}"
        
        forward_headers = {k: v for k, v in request.headers if k.lower() not in ['host', 'accept-encoding']}
        forward_headers['Host'] = urlparse(base_url).netloc
        
        forward_params = request.args.copy()
        forward_params['api_key'] = api_key
        
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            params=forward_params,
            data=request.get_data(),
            stream=True,
            timeout=30.0
        )
        
        excluded_resp_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        response_headers = [(name, value) for name, value in resp.raw.headers.items() if name.lower() not in excluded_resp_headers]
        
        return Response(resp.iter_content(chunk_size=8192), resp.status_code, response_headers)
        
    except Exception as e:
        logger.error(f"[PROXY] HTTP 代理时发生未知错误: {e}", exc_info=True)
        return "Internal Server Error", 500