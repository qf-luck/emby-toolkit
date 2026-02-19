# handler/p115_service.py
import logging
import threading
import time
from database import settings_db
try:
    from p115client import P115Client
except ImportError:
    P115Client = None

logger = logging.getLogger(__name__)

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
        config = settings_db.get_setting('nullbr_config') or {}
        cookies = config.get('p115_cookies')
        
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
            interval = int(config.get('request_interval', 5))
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
        config = settings_db.get_setting('nullbr_config') or {}
        return config.get('p115_cookies')