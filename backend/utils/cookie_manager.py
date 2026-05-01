import os
import time
import glob
import threading

class CookieManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.cookies = {} 
        self.proxy_to_cookie = {} 
        self._load_cookies()
        
    def _load_cookies(self):
        cookie_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cookies"))
        os.makedirs(cookie_dir, exist_ok=True)
        
        cookie_files = glob.glob(os.path.join(cookie_dir, "*.txt"))
        render_cookie = "/etc/secrets/youtube_cookies.txt"
        
        if os.path.exists(render_cookie) and render_cookie not in cookie_files:
            cookie_files.append(render_cookie)
            
        for i, filepath in enumerate(cookie_files):
            cid = f"cookie_{i}"
            self.cookies[cid] = {
                "file": filepath,
                "status": "active",
                "cooldown_until": 0,
                "usage_count": 0
            }
        print(f"[CookieManager] Loaded {len(self.cookies)} cookies.")
            
    def get_cookie_for_proxy(self, proxy):
        """Returns the sticky cookie path for this proxy (if available under max usage/cooldown limit)."""
        with self.lock:
            if proxy in self.proxy_to_cookie:
                cid = self.proxy_to_cookie[proxy]
                cdata = self.cookies.get(cid)
                if cdata and cdata["status"] == "active":
                    if time.time() > cdata["cooldown_until"]:
                        if cdata["usage_count"] < 2:
                            cdata["usage_count"] += 1
                            return cdata["file"]
                        else:
                            print(f"[CookieManager] Cookie {cid} hit max consecutive uses (2). Entering cooldown.")
                            self._trigger_cooldown(cid, 300)
                    else:
                        return None
            
            # Map a fresh active cookie uniquely
            now = time.time()
            assigned_cids = set(self.proxy_to_cookie.values())
            for cid, cdata in self.cookies.items():
                # Cookie must not be assigned to ANY active proxy and must be off cooldown!
                if cid not in assigned_cids and cdata["status"] == "active" and now > cdata["cooldown_until"]:
                    self.proxy_to_cookie[proxy] = cid
                    cdata["usage_count"] = 1
                    print(f"[CookieManager] ✅ Assigned fresh cookie {cid} to proxy {proxy}.")
                    return cdata["file"]
            
            return None
            
    def mark_invalid(self, proxy):
        """Called if a cookie hits LOGIN_REQUIRED while being used. Invalidates it."""
        with self.lock:
            if proxy in self.proxy_to_cookie:
                cid = self.proxy_to_cookie[proxy]
                if cid in self.cookies:
                    self.cookies[cid]["status"] = "invalid"
                    print(f"[CookieManager] ❌ Cookie {cid} invalidated due to LOGIN_REQUIRED via {proxy}.")
                del self.proxy_to_cookie[proxy]
                
    def apply_cooldown(self, proxy, seconds=300):
        """Forces the cookie to wait even if it was successful."""
        with self.lock:
            if proxy in self.proxy_to_cookie:
                cid = self.proxy_to_cookie[proxy]
                self._trigger_cooldown(cid, seconds)
                
    def _trigger_cooldown(self, cid, seconds):
        if cid in self.cookies:
            self.cookies[cid]["cooldown_until"] = time.time() + seconds
            self.cookies[cid]["usage_count"] = 0
            print(f"[CookieManager] ⏳ Cookie {cid} cooling down for {seconds}s.")

cookie_manager = CookieManager()
