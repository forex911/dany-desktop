import random
import threading

class ProxyManager:
    def __init__(self):
        self.lock = threading.Lock()
        
        # Original proxy list
        raw_proxies = [
            "31.59.20.176:6754:haqbanql:dtwi0dpaehhp",
            "198.23.239.134:6540:haqbanql:dtwi0dpaehhp",
            "45.38.107.97:6014:haqbanql:dtwi0dpaehhp",
            "107.172.163.27:6543:haqbanql:dtwi0dpaehhp",
            "198.105.121.200:6462:haqbanql:dtwi0dpaehhp",
            "216.10.27.159:6837:haqbanql:dtwi0dpaehhp",
            "142.111.67.146:5611:haqbanql:dtwi0dpaehhp",
            "191.96.254.138:6185:haqbanql:dtwi0dpaehhp",
            "31.58.9.4:6077:haqbanql:dtwi0dpaehhp",
            "23.26.71.145:5628:haqbanql:dtwi0dpaehhp",
        ]
        
        # Format for yt-dlp: http://user:pass@ip:port
        self.all_proxies = []
        for p in raw_proxies:
            ip, port, user, pwd = p.split(":")
            self.all_proxies.append(f"http://{user}:{pwd}@{ip}:{port}")
            
        self.active_proxies = list(self.all_proxies)
        self.failed_proxies = {} # {proxy: cooldown_until_timestamp}
        
    def get_random_proxy(self):
        with self.lock:
            import time
            now = time.time()
            # 1. Re-evaluate failed proxies
            recovered = []
            for proxy, cooldown_until in list(self.failed_proxies.items()):
                if now > cooldown_until:
                    recovered.append(proxy)
                    
            for p in recovered:
                self.active_proxies.append(p)
                del self.failed_proxies[p]
                print(f"[ProxyManager] ♻️ Proxy {p} recovered from cooldown and rejoined the pool.")
                
            if not self.active_proxies:
                print("\n[ProxyManager] 🔄⚠️ All active proxies are on cooldown. Forcing emergency pool reset!")
                self.reset()
                
            return random.choice(self.active_proxies)
            
    def mark_failed(self, proxy, cooldown_seconds=300):
        if not proxy: return
        with self.lock:
            import time
            if proxy in self.active_proxies:
                self.active_proxies.remove(proxy)
                self.failed_proxies[proxy] = time.time() + cooldown_seconds
                print(f"[ProxyManager] ❌ Proxy temporarily blocked. On cooldown for {cooldown_seconds}s. Active remaining: {len(self.active_proxies)}")
                
    def reset(self):
        with self.lock:
            self.active_proxies = list(self.all_proxies)
            self.failed_proxies.clear()
            
proxy_manager = ProxyManager()
