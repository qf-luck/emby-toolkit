# routes/p115.py
import logging
from flask import redirect
import json
import time
from flask import Blueprint, jsonify, request
from extensions import admin_required
from database import settings_db
from handler.p115_service import P115Service
import constants

p115_bp = Blueprint('p115_bp', __name__, url_prefix='/api/p115')
logger = logging.getLogger(__name__)

@p115_bp.route('/status', methods=['GET'])
@admin_required
def get_115_status():
    """检查 115 Cookie 状态"""
    try:
        # P115Service 内部已改为读取全局配置
        from handler.p115_service import get_115_account_info
        info = get_115_account_info()
        return jsonify({"status": "success", "data": info})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@p115_bp.route('/dirs', methods=['GET'])
@admin_required
def list_115_directories():
    """获取 115 目录列表"""
    client = P115Service.get_client()
    if not client:
        return jsonify({"status": "error", "message": "无法初始化 115 客户端，请检查 Cookies"}), 500
        
    # 二次检查 Cookies 是否存在 (虽然 get_client 已经检查过了)
    if not P115Service.get_cookies():
        return jsonify({"success": False, "message": "未配置 Cookies (请在通用设置 -> 115网盘 中配置)"}), 400

    try:
        cid = int(request.args.get('cid', 0))
    except:
        cid = 0
    
    try:
        # nf=1: 只返回文件夹
        resp = client.fs_files({
            'cid': cid, 
            'limit': 1000, 
            'asc': 1, 
            'o': 'file_name',
            'nf': 1 
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
        
        current_name = '根目录'
        if cid != 0 and resp.get('path'):
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

@p115_bp.route('/mkdir', methods=['POST'])
@admin_required
def create_115_directory():
    """创建 115 目录"""
    data = request.json
    pid = data.get('pid') or data.get('cid')
    name = data.get('name')
    
    if not name:
        return jsonify({"status": "error", "message": "目录名称不能为空"}), 400
        
    client = P115Service.get_client()
    if not client:
        return jsonify({"status": "error", "message": "无法初始化 115 客户端"}), 500
        
    try:
        resp = client.fs_mkdir(name, pid)
        if resp.get('state'):
            return jsonify({"status": "success", "data": resp})
        else:
            return jsonify({"status": "error", "message": resp.get('error_msg', '创建失败')}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@p115_bp.route('/sorting_rules', methods=['GET', 'POST'])
@admin_required
def handle_sorting_rules():
    """管理 115 分类规则"""
    if request.method == 'GET':
        raw_rules = settings_db.get_setting(constants.DB_KEY_115_SORTING_RULES)
        rules = []
        if raw_rules:
            if isinstance(raw_rules, list):
                rules = raw_rules
            elif isinstance(raw_rules, str):
                try:
                    parsed = json.loads(raw_rules)
                    if isinstance(parsed, list):
                        rules = parsed
                except Exception as e:
                    logger.error(f"解析分类规则 JSON 失败: {e}")
        
        # 确保每个规则都有 id
        for r in rules:
            if 'id' not in r:
                r['id'] = str(int(time.time() * 1000))
                
        return jsonify(rules)
    
    if request.method == 'POST':
        rules = request.json
        if not isinstance(rules, list):
            rules = []
        settings_db.save_setting(constants.DB_KEY_115_SORTING_RULES, rules)
        return jsonify({"status": "success", "message": "115 分类规则已保存"})
    
@p115_bp.route('/play/<pick_code>', methods=['GET'])
def play_115_video(pick_code):
    client = P115Service.get_client()
    if not client:
        return "115 Client Not Initialized", 500
        
    try:
        # 1. 获取调用方的 User-Agent
        # 115 的链接生成算法往往会绑定 UA，如果不传，默认可能用了 python-requests 的 UA
        ua = request.headers.get('User-Agent')
        
        # 2. 调用接口，传入 user_agent 参数
        # 注意：根据定义，它返回的是 P115URL 对象
        url_info = client.download_url(pick_code, user_agent=ua)
        
        # 3. 这里的 url_info 可能是个对象也可能是个字符串，取决于库的实现
        # 如果 url_info 直接就是链接字符串：
        real_url = str(url_info) 
        
        if not real_url or "http" not in real_url:
            logger.error(f"  ❌ 无法获取直链内容，返回值为: {url_info}")
            return "Cannot get video stream from 115", 404
            
        logger.info(f"  🎬 [直链解析成功] 302 重定向中...")
        
        # 4. 关键：有些 115 链接要求 Header 必须一致
        # 我们返回给播放器时，最好让它知道我们也拿到了这个链接
        return redirect(real_url, code=302)
        
    except Exception as e:
        logger.error(f"  ❌ 直链解析异常: {e}")
        return str(e), 500