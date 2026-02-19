# routes/nullbr.py
import logging
import json
from time import time
from flask import Blueprint, jsonify, request
from extensions import admin_required
from database import settings_db
import handler.nullbr as nullbr_handler
try:
    from p115client import P115Client
except ImportError:
    P115Client = None

nullbr_bp = Blueprint('nullbr_bp', __name__, url_prefix='/api/nullbr')
logger = logging.getLogger(__name__)

@nullbr_bp.route('/config', methods=['GET', 'POST'])
@admin_required
def handle_config():
    if request.method == 'GET':
        config = settings_db.get_setting('nullbr_config') or {}
        if 'filters' not in config:
            config['filters'] = {
                "resolutions": [], "qualities": [],
                "movie_min_size": 0, "movie_max_size": 0,
                "tv_min_size": 0, "tv_max_size": 0,
                "require_zh": False, "containers": []
            }
        if 'enabled_sources' not in config:
            config['enabled_sources'] = ['115', 'magnet', 'ed2k']
        
        if 'p115_cookies' not in config: config['p115_cookies'] = ''
        if 'p115_save_path_cid' not in config: config['p115_save_path_cid'] = 0
        if 'p115_save_path_name' not in config: config['p115_save_path_name'] = '根目录'
        if 'enable_smart_organize' not in config: config['enable_smart_organize'] = False
        if 'request_interval' not in config: config['request_interval'] = 5
        return jsonify(config)
    
    if request.method == 'POST':
        data = request.json
        new_config = {
            "api_key": data.get('api_key', '').strip(),
            "cms_url": data.get('cms_url', '').strip(),     
            "cms_token": data.get('cms_token', '').strip(),
            "p115_cookies": data.get('p115_cookies', '').strip(),
            "p115_save_path_cid": data.get('p115_save_path_cid', 0),
            "p115_save_path_name": data.get('p115_save_path_name', '根目录').strip(),
            "request_interval": int(data.get('request_interval', 5)),
            "filters": data.get('filters', {}),
            "enable_smart_organize": data.get('enable_smart_organize', False),
            "enabled_sources": data.get('enabled_sources', ['115', 'magnet', 'ed2k']),
            "updated_at": "now"
        }
        settings_db.save_setting('nullbr_config', new_config)
        return jsonify({"status": "success", "message": "配置已保存"})

@nullbr_bp.route('/user/info', methods=['GET'])
@admin_required
def get_user_info():
    """获取 NULLBR 用户信息"""
    try:
        info = nullbr_handler.get_user_info()
        return jsonify({"status": "success", "data": info})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@nullbr_bp.route('/user/redeem', methods=['POST'])
@admin_required
def redeem_code():
    """兑换激活码"""
    data = request.json
    code = data.get('code')
    if not code:
        return jsonify({"status": "error", "message": "请输入兑换码"}), 400
    
    try:
        result = nullbr_handler.redeem_code(code)
        if result.get('success'):
            return jsonify({"status": "success", "data": result})
        else:
            return jsonify({"status": "error", "message": result.get('message')}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@nullbr_bp.route('/search', methods=['POST'])
@admin_required
def search_resources():
    data = request.json
    keyword = data.get('keyword')
    page = data.get('page', 1)
    if not keyword: return jsonify({"status": "error", "message": "关键词为空"}), 400
    try:
        result = nullbr_handler.search_media(keyword, page)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@nullbr_bp.route('/resources', methods=['POST'])
@admin_required
def get_resources():
    data = request.json
    try:
        resource_list = nullbr_handler.fetch_resource_list(
            data.get('tmdb_id') or data.get('id'), 
            data.get('media_type', 'movie'), 
            specific_source=data.get('source_type'), 
            season_number=data.get('season_number'),
            episode_number=data.get('episode_number')
        )
        return jsonify({"status": "success", "data": resource_list, "total": len(resource_list)})
    except Exception as e:
        if "配额" in str(e):
            return jsonify({"status": "error", "message": str(e), "code": 402}), 402
        return jsonify({"status": "error", "message": str(e)}), 500

@nullbr_bp.route('/push', methods=['POST'])
@admin_required
def push_resource():
    data = request.json
    try:
        nullbr_handler.handle_push_request(
            data.get('link'), 
            data.get('title', '未知资源'),
            tmdb_id=data.get('tmdb_id'),
            media_type=data.get('media_type')
        )
        return jsonify({"status": "success", "message": "已添加至 115 离线任务"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@nullbr_bp.route('/presets', methods=['GET', 'POST', 'DELETE'])
@admin_required
def handle_presets():
    if request.method == 'GET':
        return jsonify(nullbr_handler.get_preset_lists())
    if request.method == 'POST':
        data = request.json
        presets = [{"id": str(i.get('id')).strip(), "name": str(i.get('name')).strip()} for i in data.get('presets', []) if i.get('id') and i.get('name')]
        settings_db.save_setting('nullbr_presets', presets)
        return jsonify({"status": "success", "message": "片单已保存"})
    if request.method == 'DELETE':
        settings_db.delete_setting('nullbr_presets')
        return jsonify({"status": "success", "message": "已恢复默认", "data": nullbr_handler.get_preset_lists()})

@nullbr_bp.route('/list', methods=['POST'])
@admin_required
def get_list_content():
    data = request.json
    try:
        result = nullbr_handler.fetch_list_items(data.get('list_id'), data.get('page', 1))
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@nullbr_bp.route('/115/status', methods=['GET'])
@admin_required
def get_115_status():
    try:
        info = nullbr_handler.get_115_account_info()
        return jsonify({"status": "success", "data": info})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@nullbr_bp.route('/sorting_rules', methods=['GET', 'POST'])
@admin_required
def handle_sorting_rules():
    if request.method == 'GET':
        # 从数据库获取
        raw_rules = settings_db.get_setting('nullbr_sorting_rules')
        
        rules = []
        if raw_rules:
            if isinstance(raw_rules, list):
                rules = raw_rules
            elif isinstance(raw_rules, str):
                try:
                    # 尝试解析 JSON 字符串
                    parsed = json.loads(raw_rules)
                    if isinstance(parsed, list):
                        rules = parsed
                except Exception as e:
                    logger.error(f"解析分类规则 JSON 失败: {e}")
        
        # 确保每个规则都有 id (前端 key 需要)
        for r in rules:
            if 'id' not in r:
                r['id'] = str(int(time.time() * 1000))
                
        return jsonify(rules)
    
    if request.method == 'POST':
        rules = request.json
        # 确保保存的是列表对象，settings_db 内部会处理序列化
        if not isinstance(rules, list):
            rules = []
        settings_db.save_setting('nullbr_sorting_rules', rules)
        return jsonify({"status": "success", "message": "整理规则已保存"})
    
@nullbr_bp.route('/115/dirs', methods=['GET'])
@admin_required
def list_115_directories():
    """
    获取 115 目录列表
    参数: cid (默认为 0)
    """
    if P115Client is None:
        return jsonify({"success": False, "message": "未安装 p115client"}), 500
        
    config = nullbr_handler.get_config()
    cookies = config.get('p115_cookies')
    if not cookies:
        return jsonify({"success": False, "message": "未配置 Cookies"}), 400

    try:
        cid = int(request.args.get('cid', 0))
    except:
        cid = 0
    
    try:
        client = P115Client(cookies)
        
        # ★★★ 核心修改：添加 nf=1 (只显示文件夹)，limit 设为 1000 ★★★
        # asc=1: 升序, o='file_name': 按文件名排序
        resp = client.fs_files({
            'cid': cid, 
            'limit': 20, 
            'asc': 1, 
            'o': 'file_name',
            'nf': 1  # <--- 关键：只返回文件夹，忽略文件
        })
        
        if not resp.get('state'):
            return jsonify({"success": False, "message": resp.get('error_msg', '获取失败')}), 500
            
        data = resp.get('data', [])
        dirs = []
        
        for item in data:
            # 双重保险：虽然加了 nf=1，还是判断一下是否有 fid
            if not item.get('fid'): 
                dirs.append({
                    "id": item.get('cid'),
                    "name": item.get('n'),
                    "parent_id": item.get('pid')
                })
        
        # 获取当前目录的路径信息 (用于面包屑)
        current_name = '根目录'
        if cid != 0 and resp.get('path'):
            # path 列表最后一个通常是当前目录
            current_name = resp.get('path')[-1].get('name', '未知目录')
                
        return jsonify({
            "success": True, 
            "data": dirs,
            "current": {
                "id": str(cid),
                "name": current_name
            }
        })
        
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    
@nullbr_bp.route('/115/mkdir', methods=['POST'])
@admin_required
def create_115_directory():
    data = request.json
    pid = data.get('pid') or data.get('cid') # 父目录ID
    name = data.get('name')
    
    if not name:
        return jsonify({"status": "error", "message": "目录名称不能为空"}), 400
        
    if P115Client is None:
        return jsonify({"status": "error", "message": "未安装 p115client"}), 500
        
    config = settings_db.get_setting('nullbr_config') or {}
    cookies = config.get('p115_cookies')
    
    try:
        client = P115Client(cookies)
        resp = client.fs_mkdir(name, pid)
        
        if resp.get('state'):
            return jsonify({"status": "success", "data": resp})
        else:
            return jsonify({"status": "error", "message": resp.get('error_msg', '创建失败')}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500