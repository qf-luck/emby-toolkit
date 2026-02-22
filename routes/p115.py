# routes/p115.py
import logging
from flask import redirect
import threading
from datetime import datetime, timedelta
import json
import os
import re
import time
from flask import Blueprint, jsonify, request, redirect
from extensions import admin_required
from database import settings_db
from handler.p115_service import P115Service, get_config
import constants
from functools import lru_cache, wraps
p115_bp = Blueprint('p115_bp', __name__, url_prefix='/api/p115')
logger = logging.getLogger(__name__)

# --- 简单的令牌桶/计数器限流器 ---
class RateLimiter:
    def __init__(self, max_requests=3, period=2):
        self.max_requests = max_requests  # 周期内最大请求数
        self.period = period              # 周期（秒）
        self.tokens = max_requests
        self.last_sync = datetime.now()
        self.lock = threading.Lock()

    def consume(self):
        with self.lock:
            now = datetime.now()
            # 补充令牌
            elapsed = (now - self.last_sync).total_seconds()
            self.tokens = min(self.max_requests, self.tokens + elapsed * (self.max_requests / self.period))
            self.last_sync = now

            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False

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
    

# 实例化限流器：建议 2 秒内最多允许 3 次解析请求（针对 115 比较稳妥）
api_limiter = RateLimiter(max_requests=3, period=2)
# 全局解析锁：确保同一时间只有一个线程在请求 115 API，防止并发冲突
fetch_lock = threading.Lock()    
@lru_cache(maxsize=2048)
def _get_cached_115_url(pick_code, user_agent, client_ip=None):
    """
    带缓存的 115 直链获取器
    """
    client = P115Service.get_client()
    if not client: return None
    # 使用锁：即使缓存失效，多个请求同时进来，也只有一个能去查 115 API
    with fetch_lock:
        # 这里的限流逻辑：如果令牌不足，直接等待或返回
        if not api_limiter.consume():
            logger.warning(f"  ⚠️ [流控] 请求过快，已拦截 pick_code: {pick_code}")
            time.sleep(0.5) # 稍微强制延迟，缓解压力
            
        try:
            # 增加一个小随机延迟，模拟人为行为
            time.sleep(0.1) 
            url_obj = client.download_url(pick_code, user_agent=user_agent)
            logger.info(f"  🎬 [115 API] 获取直链成功: {url_obj.name}")
            return str(url_obj) if url_obj else None
        except Exception as e:
            logger.error(f"  ❌ 获取 115 直链 API 报错: {e}")
            return None

@p115_bp.route('/play/<pick_code>', methods=['GET', 'HEAD']) # 允许 HEAD 请求，加速客户端嗅探
def play_115_video(pick_code):
    """
    终极极速 302 直链解析服务 (带内存缓存版)
    """
    if request.method == 'HEAD':
        # HEAD 请求通常是播放器嗅探，直接返回 200 或简单处理，不触发解析
        return '', 200

    try:
        player_ua = request.headers.get('User-Agent', 'Mozilla/5.0')
        
        # 尝试从缓存获取
        real_url = _get_cached_115_url(pick_code, player_ua)
        
        if not real_url:
            # 如果解析太快被拦截了，给播放器返回 429 告知稍后再试
            return "Too Many Requests - 115 API Protection", 429
            
        return redirect(real_url, code=302)
        
    except Exception as e:
        logger.error(f"  ❌ 直链解析发生异常: {e}")
        return str(e), 500
    
@p115_bp.route('/fix_strm', methods=['POST'])
@admin_required
def fix_strm_files():
    """扫描并修正本地所有 .strm 文件的内部链接 (支持兼容 CMS 老格式)"""
    config = get_config()
    local_root = config.get(constants.CONFIG_OPTION_LOCAL_STRM_ROOT)
    etk_url = config.get(constants.CONFIG_OPTION_ETK_SERVER_URL, "").rstrip('/')
    
    if not local_root or not os.path.exists(local_root):
        return jsonify({"success": False, "message": "未配置本地 STRM 根目录，或该目录在容器中不存在！"}), 400
    if not etk_url:
        return jsonify({"success": False, "message": "未配置 ETK 内部访问地址！"}), 400
        
    fixed_count = 0
    skipped_count = 0
    
    try:
        # 递归遍历整个本地 STRM 目录
        for root_dir, _, files in os.walk(local_root):
            for file in files:
                if file.endswith('.strm'):
                    file_path = os.path.join(root_dir, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read().strip()
                        
                        pick_code = None
                        
                        # ----------------------------------------------------
                        # ★ 核心升级：多模式兼容提取 pick_code
                        # ----------------------------------------------------
                        
                        # 模式 1: ETK 现在的标准格式
                        # 例: http://192.168.31.177:5257/api/p115/play/abc1234
                        if '/api/p115/play/' in content:
                            pick_code = content.split('/api/p115/play/')[-1].split('?')[0].strip()
                            
                        # 模式 2: ETK 之前测试用的假协议格式
                        # 例: etk_direct_play://abc1234/文件名.mkv
                        elif content.startswith('etk_direct_play://'):
                            pick_code = content.split('//')[1].split('/')[0].strip()
                            
                        # 模式 3: CMS 生成的经典格式 (增强版兼容)
                        # 解析逻辑：提取 /d/ 后面，直到出现 . 或 ? 或 / 之前的字符
                        elif '/d/' in content:
                            # 这里的正则改成了匹配 /d/ 后面非特殊符号的部分
                            match = re.search(r'/d/([a-zA-Z0-9]+)[.?/]', content)
                            if not match:
                                # 如果后面没接符号，尝试匹配到字符串结尾
                                match = re.search(r'/d/([a-zA-Z0-9]+)$', content)
                                
                            if match:
                                pick_code = match.group(1)
                                
                        # ----------------------------------------------------
                            
                        if pick_code:
                            # 拼接为当前最新的 etk_url 格式
                            new_content = f"{etk_url}/api/p115/play/{pick_code}"
                            
                            # 只有当内容确实发生变化时才执行写入
                            if content != new_content:
                                with open(file_path, 'w', encoding='utf-8') as f:
                                    f.write(new_content)
                                fixed_count += 1
                            else:
                                skipped_count += 1
                        else:
                            logger.warning(f"  ⚠️ 无法识别该 strm 格式，已跳过: {file_path}")
                            
                    except Exception as e:
                        logger.error(f"  ❌ 处理文件 {file_path} 失败: {e}")
        
        msg = f"洗刷完毕！成功修正了 {fixed_count} 个文件"
        if skipped_count > 0:
            msg += f" (已跳过 {skipped_count} 个无需修改的文件)"
        logger.info(f"  🧹 [转换完毕] {msg}")
        return jsonify({"success": True, "message": msg})
        
    except Exception as e:
        logger.error(f"  ❌ 批量修正异常: {e}", exc_info=True)
        return jsonify({"success": False, "message": str(e)}), 500