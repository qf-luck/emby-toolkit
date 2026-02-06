# tasks/watchlist.py
# 智能追剧列表任务模块
import json
import time
import logging
from typing import Optional
import concurrent.futures

# 导入需要的底层模块和共享实例
import constants
import extensions
import task_manager
from database import watchlist_db, request_db
from database.connection import get_db_connection
from handler import emby
from psycopg2.extras import execute_values
from watchlist_processor import STATUS_WATCHING, STATUS_PAUSED, STATUS_COMPLETED

logger = logging.getLogger(__name__)

# --- 追剧 ---    
def task_process_watchlist(processor, tmdb_id: Optional[str] = None):
    """
    只负责刷新“活跃”剧集（追剧中、待定中、暂停到期）。
    """
    def progress_updater(progress, message):
        task_manager.update_status_from_thread(progress, message)

    try:
        processor.run_regular_processing_task_concurrent(
            progress_callback=progress_updater, 
            tmdb_id=tmdb_id
        )

    except Exception as e:
        task_name = "追剧列表更新"
        if tmdb_id: task_name = f"单项追剧更新 (TMDb ID: {tmdb_id})"
        logger.error(f"执行 '{task_name}' 时发生顶层错误: {e}", exc_info=True)
        progress_updater(-1, f"启动任务时发生错误: {e}")

# --- 全量刷新已完结剧集任务 ---
def task_refresh_completed_series(processor):
    """
    【低频任务】全量刷新已完结剧集并检查是否有新季上线。
    """
    # 定义一个可以传递给处理器的回调函数
    def progress_updater(progress, message):
        task_manager.update_status_from_thread(progress, message)

    try:
        # 直接调用 processor 实例的方法，并将回调函数传入
        processor.refresh_completed_series_task(progress_callback=progress_updater)

    except Exception as e:
        task_name = "已完结剧集复活检查"
        logger.error(f"执行 '{task_name}' 时发生顶层错误: {e}", exc_info=True)
        progress_updater(-1, f"启动任务时发生错误: {e}")

# --- 一键扫描库内剧集任务 ---
def task_add_all_series_to_watchlist(processor):
    """
    【V4 - 全量导入+校准版】
    1. 从 Emby 获取所有剧集，确保它们都在数据库中。
    2. 扫描库内未纳管的剧集，将其直接标记为"已完结"。
    3. 获取指定库内所有"已完结且非强制"的剧集（包含新导入的和旧的）。
    4. 对这些剧集执行并发状态校准，将"假完结"修正为"追剧中"。
    """
    task_name = "一键扫描库内剧集"
    logger.trace(f"--- 开始执行 '{task_name}' (全量导入 + 状态校准) ---")

    try:
        # 1. 读取配置
        task_manager.update_status_from_thread(5, "正在读取配置...")
        library_ids_to_process = processor.config.get(constants.CONFIG_OPTION_EMBY_LIBRARIES_TO_PROCESS, [])

        if library_ids_to_process:
            logger.info(f"  ➜ 已启用媒体库过滤器: {library_ids_to_process}")
        else:
            logger.info("  ➜ 未指定媒体库，将扫描所有剧集。")

        # ★★★ 新增步骤：从 Emby 获取所有剧集并导入数据库 ★★★
        task_manager.update_status_from_thread(8, "正在从 Emby 获取所有剧集...")

        # 获取媒体库名称映射和ID列表
        library_name_map = {}
        effective_library_ids = library_ids_to_process
        try:
            all_libs = emby.get_user_accessible_libraries(
                processor.emby_url, processor.emby_api_key, processor.emby_user_id
            )
            if all_libs:
                library_name_map = {str(lib.get('Id')): lib.get('Name', '') for lib in all_libs}
                # 如果未指定媒体库，使用所有可用的媒体库
                if not effective_library_ids:
                    effective_library_ids = [str(lib.get('Id')) for lib in all_libs if lib.get('Id')]
                    logger.info(f"  ➜ 未指定媒体库，将扫描所有 {len(effective_library_ids)} 个媒体库。")
        except Exception as e:
            logger.warning(f"获取媒体库信息失败: {e}")

        # 从 Emby 获取所有剧集（使用已修复分页的函数）
        all_series = emby.get_emby_library_items(
            base_url=processor.emby_url,
            api_key=processor.emby_api_key,
            media_type_filter="Series",
            user_id=processor.emby_user_id,
            library_ids=effective_library_ids,
            library_name_map=library_name_map,
            fields="ProviderIds,Name,Type,Path"
        ) or []

        logger.info(f"  ➜ 从 Emby 获取到 {len(all_series)} 部剧集。")

        # 批量将剧集导入数据库
        if all_series:
            task_manager.update_status_from_thread(12, f"正在将 {len(all_series)} 部剧集同步到数据库...")
            imported_count = 0
            skipped_count = 0

            with get_db_connection() as conn:
                cursor = conn.cursor()
                for series in all_series:
                    tmdb_id = series.get("ProviderIds", {}).get("Tmdb")
                    if not tmdb_id:
                        skipped_count += 1
                        continue

                    item_name = series.get("Name", "未知剧集")
                    item_id = series.get("Id")
                    source_library_id = series.get("_SourceLibraryId")

                    # 构建 asset_details_json
                    asset_details = [{"emby_item_id": item_id, "source_library_id": source_library_id}] if item_id else []

                    # 使用 UPSERT 确保剧集存在于数据库中
                    sql = """
                        INSERT INTO media_metadata (tmdb_id, item_type, title, in_library, emby_item_ids_json, asset_details_json)
                        VALUES (%s, 'Series', %s, TRUE, %s, %s)
                        ON CONFLICT (tmdb_id, item_type) DO UPDATE SET
                            in_library = TRUE,
                            title = COALESCE(NULLIF(media_metadata.title, ''), EXCLUDED.title),
                            emby_item_ids_json = (
                                SELECT jsonb_agg(DISTINCT elem)
                                FROM (
                                    SELECT jsonb_array_elements_text(COALESCE(media_metadata.emby_item_ids_json, '[]'::jsonb)) AS elem
                                    UNION
                                    SELECT jsonb_array_elements_text(EXCLUDED.emby_item_ids_json) AS elem
                                ) AS combined
                                WHERE elem IS NOT NULL
                            ),
                            asset_details_json = (
                                SELECT jsonb_agg(DISTINCT elem)
                                FROM (
                                    SELECT jsonb_array_elements(COALESCE(media_metadata.asset_details_json, '[]'::jsonb)) AS elem
                                    UNION
                                    SELECT jsonb_array_elements(EXCLUDED.asset_details_json) AS elem
                                ) AS combined
                            )
                    """
                    try:
                        cursor.execute(sql, (
                            str(tmdb_id),
                            item_name,
                            json.dumps([item_id]) if item_id else '[]',
                            json.dumps(asset_details)
                        ))
                        imported_count += 1
                    except Exception as e:
                        logger.debug(f"导入剧集 {item_name} 时出错: {e}")

                conn.commit()

            logger.info(f"  ➜ 已同步 {imported_count} 部剧集到数据库，跳过 {skipped_count} 部（无 TMDb ID）。")

        # 2. 执行数据库更新 (导入为 Completed)
        task_manager.update_status_from_thread(18, "正在更新剧集状态...")
        try:
            watchlist_db.batch_import_series_as_completed(library_ids_to_process)
        except Exception as e_db:
            raise RuntimeError(f"数据库执行失败: {e_db}")

        # 3. 获取所有需要校准的候选剧集
        # 目标：所有在目标库内、状态为 Completed、且没有被用户强制完结的剧集
        task_manager.update_status_from_thread(25, "正在筛选需要校准的剧集...")
        
        condition_sql = """
            watching_status = 'Completed' 
            AND force_ended = FALSE
            AND (watchlist_tmdb_status = 'Returning Series' OR watchlist_tmdb_status IS NULL)
        """
        
        # 使用 include_all_series=True 确保我们能查到 Completed 状态的剧
        # 注意：这里复用了 watchlist_db.get_series_by_dynamic_condition
        candidates = watchlist_db.get_series_by_dynamic_condition(
            condition_sql=condition_sql,
            library_ids=library_ids_to_process,
            include_all_series=True 
        )

        total_count = len(candidates)
        
        if total_count > 0:
            logger.info(f'  ➜ 筛选出 {total_count} 部"已完结"剧集（含新旧），准备进行状态校准...')
            task_manager.update_status_from_thread(30, f"准备校准 {total_count} 部剧集的状态...")

            time.sleep(1)

            # 4. 并发执行校准
            processed_count = 0

            def worker(series_data):
                try:
                    # 调用单体处理逻辑，它会自动修正状态 (Completed -> Watching)
                    processor._process_one_series(series_data)
                except Exception as e:
                    logger.error(f"校准剧集 {series_data.get('item_name')} 失败: {e}")

            # 使用线程池
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(worker, s) for s in candidates]

                for future in concurrent.futures.as_completed(futures):
                    processed_count += 1
                    # 进度条从 30% 走到 100%
                    progress = 30 + int((processed_count / total_count) * 70)
                    # 每处理5个或者最后时刻更新一次UI，避免太频繁
                    if processed_count % 5 == 0 or processed_count == total_count:
                        task_manager.update_status_from_thread(progress, f"正在校准: {processed_count}/{total_count}")
            
            final_message = f"任务完成：已扫描并校准了 {total_count} 部剧集的状态。"
            logger.info(f"  ➜ {final_message}")
            task_manager.update_status_from_thread(100, final_message)

        else:
            final_message = "扫描完成！没有发现需要处理的剧集。"
            logger.info(f"  ➜ {final_message}")
            task_manager.update_status_from_thread(100, final_message)

    except Exception as e:
        logger.error(f"执行 '{task_name}' 任务时发生严重错误: {e}", exc_info=True)
        task_manager.update_status_from_thread(-1, f"任务失败: {e}")

# --- 缺集扫描任务 ---
def task_scan_library_gaps(processor):
    """
    【V2 - 数据库直通版】
    - 缺集扫描任务。
    - 优化：直接从数据库获取符合条件（已完结/未追踪 + 未忽略 + 指定库）的剧集，
      不再获取全量数据后在 Python 中过滤。
    """
    task_name = "媒体库缺集扫描"
    logger.trace(f"--- 开始执行 '{task_name}' 任务 (高效模式) ---")
    
    def progress_updater(progress, message):
        task_manager.update_status_from_thread(progress, message)

    try:
        # 1. 获取配置的媒体库
        progress_updater(5, "正在读取配置...")
        library_ids = processor.config.get(constants.CONFIG_OPTION_EMBY_LIBRARIES_TO_PROCESS, [])
        
        if library_ids:
            logger.info(f"  ➜ 已启用媒体库过滤器: {library_ids}")

        # 2. ★★★ 核心优化：直接从数据库获取目标剧集 ★★★
        progress_updater(10, "正在从数据库筛选目标剧集...")
        target_series = watchlist_db.get_gap_scan_candidates(library_ids)

        if not target_series:
            progress_updater(100, "任务完成：没有符合条件（已完结/未追踪 且 未忽略）的剧集需要扫描。")
            return
        
        total_series = len(target_series)
        logger.info(f"  ➜ 数据库筛选完成，发现 {total_series} 部符合条件的剧集。")
        
        all_series_tmdb_ids = [s['tmdb_id'] for s in target_series if s.get('tmdb_id')]

        # 3. 执行本地缺集分析 (逻辑保持不变，但输入数据更精准了)
        progress_updater(20, f"正在对 {total_series} 部剧集进行本地缺集分析...")
        incomplete_seasons = watchlist_db.find_detailed_missing_episodes(all_series_tmdb_ids)

        # 4. 更新前端显示状态 (逻辑保持不变)
        progress_updater(60, "分析完成，正在更新前端显示状态...")
        gaps_by_series = {}
        for season_info in incomplete_seasons:
            series_id = season_info['parent_series_tmdb_id']
            season_num = season_info['season_number']
            missing_eps = sorted(season_info.get('missing_episodes') or [])
            gaps_by_series.setdefault(series_id, []).append({
                "season": season_num,
                "missing": missing_eps
            })
        
        final_gaps_data_to_update = {
            series_id: gaps_by_series.get(series_id, [])
            for series_id in all_series_tmdb_ids
        }
        
        if final_gaps_data_to_update:
            watchlist_db.batch_update_gaps_info(final_gaps_data_to_update)
            logger.info("  ➜ 成功将最新的详细缺集信息同步到所有被扫描的剧集中。")

        if not incomplete_seasons:
            progress_updater(100, "分析完成：目标剧集的所有季都是完整的。")
            return
            
        # 5. 提交订阅请求 (逻辑保持不变)
        total_seasons_to_sub = len(incomplete_seasons)
        logger.info(f"  ➜ 本地分析完成！共发现 {total_seasons_to_sub} 个分集不完整的季需要重新订阅。")
        progress_updater(80, f"发现 {total_seasons_to_sub} 个不完整的季，准备提交订阅请求...")

        series_info_map = {s['tmdb_id']: s for s in target_series if s.get('tmdb_id')}
        media_info_batch_for_sub = []
        
        for i, season_info in enumerate(incomplete_seasons):
            series_tmdb_id = season_info['parent_series_tmdb_id']
            season_num = season_info['season_number']
            season_tmdb_id = season_info['season_tmdb_id']
            
            if not season_tmdb_id: 
                continue

            series_info = series_info_map.get(series_tmdb_id)
            series_title = series_info.get('item_name', '未知剧集') if series_info else '未知剧集'
            
            media_info_batch_for_sub.append({
                'tmdb_id': season_tmdb_id,
                'title': f"{series_title} - 第 {season_num} 季",
                'parent_series_tmdb_id': series_tmdb_id,
                'season_number': season_num,
                'poster_path': season_info.get('season_poster_path') 
            })

        if media_info_batch_for_sub:
            request_db.set_media_status_wanted(
                tmdb_ids=[info['tmdb_id'] for info in media_info_batch_for_sub],
                item_type='Season',
                source={"type": "gap_scan", "reason": "incomplete_season"},
                media_info_list=media_info_batch_for_sub
            )

        final_message = f"任务完成！共为 {len(media_info_batch_for_sub)} 个不完整的季提交了订阅请求。"
        progress_updater(100, final_message)

    except Exception as e:
        logger.error(f"执行 '{task_name}' 时发生严重错误: {e}", exc_info=True)
        progress_updater(-1, f"任务失败: {e}")

# --- 补全旧季任务 ---
def task_scan_old_seasons_backfill(processor):
    """
    【数据库直通版】扫描所有剧集，直接订阅缺失的旧季。
    不依赖 TMDb API，不依赖复杂的 Processor 逻辑。
    """
    task_name = "补全旧季"
    
    def progress_updater(progress, message):
        task_manager.update_status_from_thread(progress, message)

    try:
        logger.trace(f"--- 开始执行 '{task_name}' ---")
        progress_updater(10, "正在分析数据库中的缺失旧季...")

        # 1. 直接从数据库获取列表
        # 这里可以传入 library_ids 如果需要限制范围，暂默认全量扫描
        missing_seasons = watchlist_db.find_missing_old_seasons()
        
        if not missing_seasons:
            progress_updater(100, "扫描完成：没有发现缺失的旧季。")
            return

        total_count = len(missing_seasons)
        logger.info(f"  ➜ 发现 {total_count} 个缺失的旧季，准备提交订阅...")
        progress_updater(50, f"发现 {total_count} 个缺失旧季，正在提交请求...")

        # 2. 构造 media_info_list
        media_info_list = []
        tmdb_ids = []
        
        for s in missing_seasons:
            tmdb_ids.append(s['tmdb_id'])
            media_info_list.append({
                'tmdb_id': s['tmdb_id'],
                'item_type': 'Season',
                'title': s.get('title') or f"{s.get('series_title', '未知剧集')} - 第 {s['season_number']} 季",
                'original_title': s.get('original_title'),
                'release_date': s.get('release_date'), # 数据库里存的是 date 对象或字符串
                'poster_path': s.get('poster_path'),
                'season_number': s['season_number'],
                'parent_series_tmdb_id': s['parent_series_tmdb_id'],
                'overview': s.get('overview'),
                # 可以在这里塞入 reason，request_db 会优先使用
                'reason': f"backfill_old_season: S{s['season_number']} Missing"
            })

        # 3. 批量提交订阅
        if tmdb_ids:
            request_db.set_media_status_wanted(
                tmdb_ids=tmdb_ids,
                item_type='Season',
                source={"type": "scan_old_seasons_backfill", "reason": "backfill_old_season"},
                media_info_list=media_info_list
            )

        final_msg = f"任务完成！已为 {total_count} 个旧季提交了订阅请求。"
        logger.info(f"  ➜ {final_msg}")
        progress_updater(100, final_msg)

    except Exception as e:
        logger.error(f"执行 '{task_name}' 时发生错误: {e}", exc_info=True)
        progress_updater(-1, f"任务失败: {e}")