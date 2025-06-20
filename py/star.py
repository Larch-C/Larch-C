#!/usr/bin/env python3
"""
GitHub Star 监控脚本（异步版本）
实时监控指定仓库的star变化，包括新增和取消的用户
使用aiohttp实现异步请求
"""

import aiohttp
import asyncio
import json
import argparse
import os
from datetime import datetime
from typing import Set, Dict, List, Optional
import logging
from pathlib import Path

class GitHubStarMonitor:
    def __init__(self, repo_owner: str, repo_name: str, token: str = None, 
                 check_interval: int = 60, log_file: str = None, 
                 state_file: str = None):
        """
        初始化监控器
        
        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            token: GitHub Personal Access Token (可选，但建议使用以避免API限制)
            check_interval: 检查间隔（秒）
            log_file: 日志文件路径
            state_file: 状态文件路径，用于持久化存储
        """
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.repo_full_name = f"{repo_owner}/{repo_name}"
        self.token = token
        self.check_interval = check_interval
        
        # 设置状态文件路径
        if state_file is None:
            safe_repo_name = self.repo_full_name.replace('/', '_')
            self.state_file = f"star_monitor_{safe_repo_name}.json"
        else:
            self.state_file = state_file
        
        # 设置请求头
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "GitHub-Star-Monitor"
        }
        if self.token:
            self.headers["Authorization"] = f"token {self.token}"
        
        # 存储当前的stargazers和详细信息
        self.current_stargazers: Set[str] = set()
        self.stargazers_info: Dict[str, Dict] = {}  # 存储用户详细信息
        self.last_check_time: Optional[str] = None
        self.total_stars: int = 0
        
        # 设置日志
        self._setup_logging(log_file)
        
        # API URL
        self.stargazers_url = f"https://api.github.com/repos/{self.repo_full_name}/stargazers"
        self.repo_url = f"https://api.github.com/repos/{self.repo_full_name}"
        
        # 创建session
        self.session = None
    
    def _setup_logging(self, log_file: str = None):
        """设置日志配置"""
        log_format = '%(asctime)s - %(levelname)s - %(message)s'
        
        if log_file:
            logging.basicConfig(
                level=logging.INFO,
                format=log_format,
                handlers=[
                    logging.FileHandler(log_file, encoding='utf-8'),
                    logging.StreamHandler()
                ]
            )
        else:
            logging.basicConfig(level=logging.INFO, format=log_format)
        
        self.logger = logging.getLogger(__name__)
    
    async def _create_session(self):
        """创建aiohttp session"""
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=timeout
            )
    
    def load_state(self) -> bool:
        """
        从文件加载状态
        
        Returns:
            bool: 是否成功加载了历史状态
        """
        try:
            if not Path(self.state_file).exists():
                self.logger.info(f"状态文件不存在: {self.state_file}")
                return False
            
            with open(self.state_file, 'r', encoding='utf-8') as f:
                state_data = json.load(f)
            
            # 验证状态文件格式
            if 'repo_full_name' not in state_data or state_data.get('repo_full_name') != self.repo_full_name:
                self.logger.warning(f"状态文件仓库不匹配，忽略历史状态")
                return False
            
            self.current_stargazers = set(state_data.get('stargazers', []))
            self.stargazers_info = state_data.get('stargazers_info', {})
            self.last_check_time = state_data.get('last_check_time')
            self.total_stars = state_data.get('total_stars', 0)
            
            self.logger.info(f"成功加载历史状态: {len(self.current_stargazers)} 个stargazers")
            if self.last_check_time:
                self.logger.info(f"上次检查时间: {self.last_check_time}")
            
            return True
            
        except Exception as e:
            self.logger.error(f"加载状态文件失败: {e}")
            return False
    
    def save_state(self):
        """保存状态到文件"""
        try:
            state_data = {
                'repo_full_name': self.repo_full_name,
                'stargazers': list(self.current_stargazers),
                'stargazers_info': self.stargazers_info,
                'last_check_time': datetime.now().isoformat(),
                'total_stars': self.total_stars,
                'save_time': datetime.now().isoformat()
            }
            
            # 创建备份文件
            backup_file = f"{self.state_file}.backup"
            if Path(self.state_file).exists():
                Path(self.state_file).rename(backup_file)
            
            # 保存新状态
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(state_data, f, ensure_ascii=False, indent=2)
            
            # 删除备份文件
            if Path(backup_file).exists():
                Path(backup_file).unlink()
            
            self.last_check_time = state_data['last_check_time']
            
        except Exception as e:
            self.logger.error(f"保存状态文件失败: {e}")
            # 如果保存失败，尝试恢复备份
            backup_file = f"{self.state_file}.backup"
            if Path(backup_file).exists():
                try:
                    Path(backup_file).rename(self.state_file)
                    self.logger.info("已恢复备份状态文件")
                except Exception as restore_error:
                    self.logger.error(f"恢复备份失败: {restore_error}")
    
    
    async def _make_request(self, url: str, params: Dict = None) -> Dict:
        """发送API请求"""
        await self._create_session()
        
        try:
            async with self.session.get(url, params=params) as response:
                # 检查API限制
                if response.status == 403:
                    response_text = await response.text()
                    if 'rate limit' in response_text.lower():
                        reset_time = response.headers.get('X-RateLimit-Reset')
                        if reset_time:
                            wait_time = int(reset_time) - int(asyncio.get_event_loop().time())
                            self.logger.warning(f"API限制已达到，等待 {wait_time} 秒后重试")
                            await asyncio.sleep(max(wait_time, 60))
                            return await self._make_request(url, params)
                
                response.raise_for_status()
                return await response.json()
                
        except aiohttp.ClientError as e:
            self.logger.error(f"请求失败: {e}")
            raise
        except Exception as e:
            self.logger.error(f"未知错误: {e}")
            raise
    
    async def get_all_stargazers(self) -> List[Dict]:
        """获取所有stargazers"""
        stargazers = []
        page = 1
        per_page = 100
        
        self.logger.info("正在获取stargazers列表...")
        
        # 创建并发任务列表
        tasks = []
        
        # 首先获取第一页来确定总页数
        params = {'page': 1, 'per_page': per_page}
        first_page_data = await self._make_request(self.stargazers_url, params)
        
        if not first_page_data:
            return []
        
        stargazers.extend(first_page_data)
        
        # 如果第一页就没满，说明只有一页
        if len(first_page_data) < per_page:
            self.logger.info(f"获取完成，共 {len(stargazers)} 个stargazers")
            return stargazers
        
        # 创建并发任务获取剩余页面
        # 先尝试获取几页来估算总页数
        max_concurrent = min(10, 50)  # 限制并发数
        page = 2
        
        while True:
            # 创建一批并发任务
            batch_tasks = []
            for i in range(max_concurrent):
                current_page = page + i
                params = {'page': current_page, 'per_page': per_page}
                task = self._make_request(self.stargazers_url, params)
                batch_tasks.append(task)
            
            # 执行当前批次
            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            
            has_data = False
            for result in batch_results:
                if isinstance(result, Exception):
                    self.logger.error(f"获取页面时出错: {result}")
                    continue
                
                if result:  # 如果有数据
                    stargazers.extend(result)
                    has_data = True
                    if len(result) < per_page:
                        # 这是最后一页
                        self.logger.info(f"获取完成，共 {len(stargazers)} 个stargazers")
                        return stargazers
            
            if not has_data:
                break
            
            page += max_concurrent
            self.logger.info(f"已获取 {len(stargazers)} 个stargazers")
            
            # 添加小延迟避免请求过于频繁
            await asyncio.sleep(0.1)
        
        self.logger.info(f"获取完成，共 {len(stargazers)} 个stargazers")
        return stargazers
    
    async def get_repo_info(self) -> Dict:
        """获取仓库基本信息"""
        return await self._make_request(self.repo_url)
    
    async def initialize_stargazers(self):
        """初始化stargazers列表"""
        try:
            # 尝试加载历史状态
            has_history = self.load_state()
            
            # 获取当前仓库信息
            repo_info = await self.get_repo_info()
            current_total_stars = repo_info['stargazers_count']
            
            if has_history:
                # 如果有历史状态，检查是否需要更新
                if abs(current_total_stars - self.total_stars) <= 10:
                    # 如果变化不大，使用历史状态并进行一次检查
                    self.logger.info(f"使用历史状态: {self.repo_full_name}")
                    self.logger.info(f"历史stars数量: {self.total_stars}, 当前: {current_total_stars}")
                    self.total_stars = current_total_stars
                    
                    # 立即进行一次检查来同步状态
                    await self.check_star_changes()
                    return
                else:
                    self.logger.info(f"stars数量变化较大 ({self.total_stars} -> {current_total_stars})，重新获取完整列表")
            
            # 获取完整的stargazers列表
            stargazers = await self.get_all_stargazers()
            self.current_stargazers = {star['login'] for star in stargazers}
            
            # 存储用户详细信息
            self.stargazers_info = {}
            for star in stargazers:
                self.stargazers_info[star['login']] = {
                    'html_url': star['html_url'],
                    'avatar_url': star['avatar_url'],
                    'id': star['id']
                }
            
            self.total_stars = current_total_stars
            
            self.logger.info(f"初始化完成: {self.repo_full_name}")
            self.logger.info(f"当前stars总数: {self.total_stars}")
            self.logger.info(f"获取到的stargazers数量: {len(self.current_stargazers)}")
            
            # 保存初始状态
            self.save_state()
            
        except Exception as e:
            self.logger.error(f"初始化失败: {e}")
            raise
    
    async def check_star_changes(self):
        """检查star变化"""
        try:
            # 并发获取当前的stargazers和仓库信息
            stargazers_task = self.get_all_stargazers()
            repo_info_task = self.get_repo_info()
            
            current_stargazers_list, repo_info = await asyncio.gather(
                stargazers_task, repo_info_task
            )
            
            new_stargazers_set = {star['login'] for star in current_stargazers_list}
            
            # 检查新增的stars
            new_stars = new_stargazers_set - self.current_stargazers
            # 检查取消的stars
            removed_stars = self.current_stargazers - new_stargazers_set
            
            # 处理新增的stars
            if new_stars:
                for username in new_stars:
                    # 获取用户详细信息
                    user_info = next((star for star in current_stargazers_list 
                                    if star['login'] == username), None)
                    if user_info:
                        # 存储用户信息
                        self.stargazers_info[username] = {
                            'html_url': user_info['html_url'],
                            'avatar_url': user_info['avatar_url'],
                            'id': user_info['id']
                        }
                        
                        self.logger.info(f"⭐ 新增Star: {username}")
                        self.logger.info(f"   用户链接: {user_info['html_url']}")
                        self.logger.info(f"   用户头像: {user_info['avatar_url']}")
                        if user_info.get('name'):
                            self.logger.info(f"   用户名称: {user_info['name']}")
            
            # 处理取消的stars
            if removed_stars:
                for username in removed_stars:
                    # 尝试从历史信息中获取用户详情
                    user_info = self.stargazers_info.get(username, {})
                    
                    self.logger.info(f"💔 取消Star: {username}")
                    self.logger.info(f"   用户链接: {user_info.get('html_url', f'https://github.com/{username}')}")
                    if user_info.get('avatar_url'):
                        self.logger.info(f"   用户头像: {user_info['avatar_url']}")
                    
                    # 从存储中移除用户信息（可选，也可以保留作为历史记录）
                    # self.stargazers_info.pop(username, None)
            
            # 更新当前stargazers集合
            self.current_stargazers = new_stargazers_set
            
            # 更新用户信息字典，添加新用户
            for star in current_stargazers_list:
                if star['login'] not in self.stargazers_info:
                    self.stargazers_info[star['login']] = {
                        'html_url': star['html_url'],
                        'avatar_url': star['avatar_url'],
                        'id': star['id']
                    }
            
            # 显示总数变化
            if new_stars or removed_stars:
                old_total = self.total_stars
                self.total_stars = repo_info['stargazers_count']
                change = len(new_stars) - len(removed_stars)
                change_str = f"+{change}" if change > 0 else str(change)
                self.logger.info(f"Stars总数变化: {change_str} ({old_total} -> {self.total_stars})")
                self.logger.info("-" * 50)
                
                # 保存状态
                self.save_state()
            else:
                # 即使没有变化也更新检查时间
                self.total_stars = repo_info['stargazers_count']
                self.save_state()
            
        except Exception as e:
            self.logger.error(f"检查star变化时出错: {e}")
    
    async def cleanup(self):
        """清理资源"""
        await self._close_session()
        # 最后保存一次状态
        self.save_state()
        self.logger.info(f"状态已保存到: {self.state_file}")
    
    async def start_monitoring(self):
        """开始监控"""
        self.logger.info(f"开始监控仓库: {self.repo_full_name}")
        self.logger.info(f"检查间隔: {self.check_interval} 秒")
        self.logger.info(f"状态文件: {self.state_file}")
        self.logger.info("=" * 50)
        
        try:
            # 初始化
            await self.initialize_stargazers()
            
            # 开始循环监控
            while True:
                await asyncio.sleep(self.check_interval)
                self.logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 检查star变化...")
                await self.check_star_changes()
                
        except KeyboardInterrupt:
            self.logger.info("监控已停止")
        except Exception as e:
            self.logger.error(f"监控过程中出错: {e}")
            raise
        finally:
            # 确保清理资源
            await self.cleanup()


async def main():
    parser = argparse.ArgumentParser(description='GitHub Star 监控器（异步版本）')
    parser.add_argument('repo', help='仓库名称，格式: owner/repo')
    parser.add_argument('--token', '-t', help='GitHub Personal Access Token')
    parser.add_argument('--interval', '-i', type=int, default=60, 
                       help='检查间隔（秒），默认60秒')
    parser.add_argument('--log-file', '-l', help='日志文件路径')
    parser.add_argument('--state-file', '-s', help='状态文件路径，默认为 star_monitor_owner_repo.json')
    
    args = parser.parse_args()
    
    # 解析仓库名称
    try:
        repo_owner, repo_name = args.repo.split('/')
    except ValueError:
        print("错误: 仓库名称格式应为 'owner/repo'")
        return
    
    # 从环境变量获取token（如果命令行没有提供）
    token = args.token or os.getenv('GITHUB_TOKEN')
    
    if not token:
        print("警告: 未提供GitHub token，API请求将受到限制")
        print("建议设置环境变量 GITHUB_TOKEN 或使用 --token 参数")
    
    # 创建监控器
    monitor = GitHubStarMonitor(
        repo_owner=repo_owner,
        repo_name=repo_name,
        token=token,
        check_interval=args.interval,
        log_file=args.log_file,
        state_file=args.state_file
    )
    
    # 开始监控
    await monitor.start_monitoring()


if __name__ == "__main__":
    asyncio.run(main())