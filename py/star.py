#!/usr/bin/env python3
"""
GitHub Star 监控脚本（异步版本，优雅退出）
实时监控指定仓库的star变化，包括新增和取消的用户
使用aiohttp实现异步请求，支持优雅退出和高级Web界面
"""

import aiohttp
import asyncio
import json
import argparse
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Set, Dict, List, Optional
import logging
from pathlib import Path
from aiohttp import web
import jinja2
from functools import lru_cache
import aiohttp_jinja2
from collections import defaultdict

class GracefulExit:
    """优雅退出处理器"""
    def __init__(self):
        self.kill_now = False
        # 注册信号处理器
        signal.signal(signal.SIGTERM, self._exit_gracefully)
        signal.signal(signal.SIGINT, self._exit_gracefully)
        if hasattr(signal, 'SIGHUP'):  # Windows 不支持 SIGHUP
            signal.signal(signal.SIGHUP, self._exit_gracefully)

    def _exit_gracefully(self, signum, frame):
        """信号处理函数"""
        signal_names = {
            signal.SIGTERM: 'SIGTERM',
            signal.SIGINT: 'SIGINT'
        }
        if hasattr(signal, 'SIGHUP'):
            signal_names[signal.SIGHUP] = 'SIGHUP'

        signal_name = signal_names.get(signum, f'Signal {signum}')
        print(f"\n收到退出信号 {signal_name}，正在优雅退出...")
        self.kill_now = True

class StarStats:
    """Star统计数据管理器"""
    def __init__(self):
        self.daily_stats = defaultdict(lambda: {"gained": 0, "lost": 0})
        self.hourly_stats = defaultdict(lambda: {"gained": 0, "lost": 0})
        self.total_gained = 0
        self.total_lost = 0
        self.milestones = [10, 50, 100, 500, 1000, 5000, 10000]
        self.last_milestone = 0

    def update(self, action: str, count: int = 1):
        """更新统计数据"""
        now = datetime.now()
        day_key = now.strftime('%Y-%m-%d')
        hour_key = now.strftime('%Y-%m-%d-%H')

        if action == 'gain':
            self.daily_stats[day_key]['gained'] += count
            self.hourly_stats[hour_key]['gained'] += count
            self.total_gained += count
        elif action == 'lost':
            self.daily_stats[day_key]['lost'] += count
            self.hourly_stats[hour_key]['lost'] += count
            self.total_lost += count

    def get_trend_data(self, days: int = 7) -> Dict:
        """获取趋势数据"""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        dates = []
        gained_data = []
        lost_data = []
        
        current_date = start_date
        while current_date <= end_date:
            date_key = current_date.strftime('%Y-%m-%d')
            dates.append(date_key)
            gained_data.append(self.daily_stats[date_key]['gained'])
            lost_data.append(self.daily_stats[date_key]['lost'])
            current_date += timedelta(days=1)
            
        return {
            'labels': dates,
            'gained': gained_data,
            'lost': lost_data
        }

class NotificationSettings:
    """通知设置管理器"""
    def __init__(self):
        self.email_enabled = False
        self.email_address = None
        self.webhook_url = None
        self.notify_on_milestone = True
        self.notify_on_large_changes = True
        self.large_change_threshold = 10  # 10颗星变化视为大变化

    async def send_notification(self, message: str):
        """发送通知"""
        if self.webhook_url:
            try:
                async with aiohttp.ClientSession() as session:
                    await session.post(self.webhook_url, json={'message': message})
            except Exception as e:
                logging.error(f"发送通知失败: {e}")

class GitHubStarMonitor:
    def __init__(self, repo_owner: str, repo_name: str, token: str = None,
                 check_interval: int = 60, log_file: str = None,
                 state_file: str = None, web_port: int = 8080):
        """
        初始化监控器

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            token: GitHub Personal Access Token (可选，但建议使用以避免API限制)
            check_interval: 检查间隔（秒）
            log_file: 日志文件路径
            state_file: 状态文件路径，用于持久化存储
            web_port: Web界面端口
        """
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.repo_full_name = f"{repo_owner}/{repo_name}"
        self.token = token
        self.check_interval = check_interval
        self.web_port = web_port
        self.app = web.Application()
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/api/stats', self.handle_stats)
        self.app.router.add_get('/api/stats/trend', self.handle_trend)
        self.app.router.add_get('/api/stats/export', self.handle_export)
        self.app.router.add_get('/api/activities', self.handle_activities)
        self.app.router.add_post('/api/settings', self.handle_settings)
        
        # 优雅退出处理器
        self.graceful_exit = GracefulExit()

        # 运行状态标志
        self.is_running = False
        self.is_shutting_down = False

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

        # 当前运行的任务列表，用于清理
        self.running_tasks: Set[asyncio.Task] = set()

        # 加载HTML模板
        self.template = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>GitHub Star Monitor - {{repo_name}}</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
            <link href="https://cdn.jsdelivr.net/npm/@mdi/font@6.5.95/css/materialdesignicons.min.css" rel="stylesheet">
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <style>
                :root {
                    --primary-color: #2563eb;
                    --primary-hover: #1d4ed8;
                    --success-color: #22c55e;
                    --danger-color: #ef4444;
                    --bg-color: #f8fafc;
                    --card-bg: #ffffff;
                    --text-color: #1e293b;
                    --text-secondary: #64748b;
                    --border-color: #e2e8f0;
                    --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
                    --shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1);
                    --shadow-lg: 0 10px 15px -3px rgb(0 0 0 / 0.1);
                }
                
                @media (prefers-color-scheme: dark) {
                    :root {
                        --bg-color: #0f172a;
                        --card-bg: #1e293b;
                        --text-color: #f1f5f9;
                        --text-secondary: #94a3b8;
                        --border-color: #334155;
                    }
                }
                
                * {
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }
                
                body { 
                    font-family: 'Inter', -apple-system, sans-serif;
                    background: var(--bg-color);
                    color: var(--text-color);
                    line-height: 1.5;
                }
                
                .container {
                    max-width: 1200px;
                    margin: 0 auto;
                    padding: 2rem;
                }
                
                .header {
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    margin-bottom: 2rem;
                }
                
                .header h1 {
                    font-size: 1.875rem;
                    font-weight: 700;
                    background: linear-gradient(to right, var(--primary-color), #7c3aed);
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                }
                
                .repo-info {
                    display: flex;
                    align-items: center;
                    gap: 0.5rem;
                    font-size: 1.25rem;
                }
                
                .repo-info i {
                    color: var(--primary-color);
                }
                
                .card {
                    background: var(--card-bg);
                    border-radius: 1rem;
                    padding: 1.5rem;
                    margin-bottom: 1.5rem;
                    box-shadow: var(--shadow);
                    border: 1px solid var(--border-color);
                    transition: all 0.3s ease;
                }
                
                .card:hover {
                    transform: translateY(-2px);
                    box-shadow: var(--shadow-lg);
                }
                
                .stats {
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
                    gap: 1rem;
                }
                
                .stat-card {
                    background: var(--card-bg);
                    padding: 1.5rem;
                    border-radius: 0.75rem;
                    border: 1px solid var(--border-color);
                    display: flex;
                    flex-direction: column;
                    gap: 0.5rem;
                }
                
                .stat-card .label {
                    color: var(--text-secondary);
                    font-size: 0.875rem;
                    font-weight: 500;
                    text-transform: uppercase;
                    letter-spacing: 0.05em;
                }
                
                .stat-card .value {
                    font-size: 2rem;
                    font-weight: 700;
                    color: var(--primary-color);
                }
                
                .stat-card .trend {
                    display: flex;
                    align-items: center;
                    gap: 0.25rem;
                    font-size: 0.875rem;
                }
                
                .trend.up { color: var(--success-color); }
                .trend.down { color: var(--danger-color); }
                
                .controls {
                    display: flex;
                    gap: 0.75rem;
                    margin: 1.5rem 0;
                    flex-wrap: wrap;
                }
                
                .btn {
                    display: inline-flex;
                    align-items: center;
                    gap: 0.5rem;
                    padding: 0.75rem 1.5rem;
                    border-radius: 0.5rem;
                    font-weight: 500;
                    font-size: 0.875rem;
                    cursor: pointer;
                    transition: all 0.2s ease;
                    border: none;
                }
                
                .btn-primary {
                    background: var(--primary-color);
                    color: white;
                }
                
                .btn-primary:hover {
                    background: var(--primary-hover);
                }
                
                .btn-outline {
                    border: 1px solid var(--border-color);
                    background: transparent;
                    color: var(--text-color);
                }
                
                .btn-outline:hover {
                    background: var(--bg-color);
                }
                
                select {
                    padding: 0.75rem;
                    border-radius: 0.5rem;
                    border: 1px solid var(--border-color);
                    background: var(--card-bg);
                    color: var(--text-color);
                    font-size: 0.875rem;
                    cursor: pointer;
                }
                
                .chart-container {
                    position: relative;
                    height: 300px;
                    margin: 1rem 0;
                }
                
                .activity-list {
                    display: flex;
                    flex-direction: column;
                    gap: 0.75rem;
                }
                
                .activity-item {
                    display: flex;
                    align-items: flex-start;
                    gap: 1rem;
                    padding: 1rem;
                    border-radius: 0.5rem;
                    background: var(--bg-color);
                    transition: all 0.2s ease;
                }
                
                .activity-item:hover {
                    transform: translateX(0.5rem);
                }
                
                .activity-icon {
                    width: 2rem;
                    height: 2rem;
                    border-radius: 50%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    background: var(--primary-color);
                    color: white;
                }
                
                .activity-content {
                    flex: 1;
                }
                
                .activity-message {
                    margin-bottom: 0.25rem;
                }
                
                .activity-time {
                    color: var(--text-secondary);
                    font-size: 0.875rem;
                }
                
                .loading {
                    position: fixed;
                    inset: 0;
                    background: rgba(0, 0, 0, 0.5);
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    z-index: 50;
                    backdrop-filter: blur(4px);
                }
                
                .loading-content {
                    background: var(--card-bg);
                    padding: 2rem;
                    border-radius: 1rem;
                    text-align: center;
                    box-shadow: var(--shadow-lg);
                }
                
                .spinner {
                    width: 2.5rem;
                    height: 2.5rem;
                    border: 3px solid var(--border-color);
                    border-top-color: var(--primary-color);
                    border-radius: 50%;
                    animation: spin 1s linear infinite;
                    margin: 0 auto 1rem;
                }
                
                @keyframes spin {
                    to { transform: rotate(360deg); }
                }
                
                .settings-panel {
                    position: fixed;
                    inset: 0;
                    background: rgba(0, 0, 0, 0.5);
                    display: none;
                    place-items: center;
                    z-index: 50;
                    backdrop-filter: blur(4px);
                }
                
                .settings-content {
                    background: var(--card-bg);
                    padding: 2rem;
                    border-radius: 1rem;
                    width: 100%;
                    max-width: 500px;
                    box-shadow: var(--shadow-lg);
                }
                
                .settings-header {
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    margin-bottom: 1.5rem;
                }
                
                .settings-form {
                    display: flex;
                    flex-direction: column;
                    gap: 1rem;
                }
                
                .form-group {
                    display: flex;
                    flex-direction: column;
                    gap: 0.5rem;
                }
                
                .form-group label {
                    font-weight: 500;
                }
                
                .form-group input[type="number"] {
                    padding: 0.75rem;
                    border-radius: 0.5rem;
                    border: 1px solid var(--border-color);
                    background: var(--bg-color);
                    color: var(--text-color);
                }
                
                .settings-actions {
                    display: flex;
                    gap: 0.75rem;
                    margin-top: 1.5rem;
                }
                
                .user-info {
                    display: flex;
                    align-items: center;
                    gap: 0.75rem;
                    margin-top: 0.5rem;
                }
                
                .user-avatar {
                    width: 2rem;
                    height: 2rem;
                    border-radius: 50%;
                    object-fit: cover;
                }
                
                .user-name {
                    color: var(--primary-color);
                    font-weight: 500;
                    text-decoration: none;
                }
                
                .user-name:hover {
                    text-decoration: underline;
                }
                
                @media (max-width: 640px) {
                    .container {
                        padding: 1rem;
                    }
                    
                    .header {
                        flex-direction: column;
                        align-items: flex-start;
                        gap: 1rem;
                    }
                    
                    .controls {
                        flex-direction: column;
                    }
                    
                    .btn {
                        width: 100%;
                        justify-content: center;
                    }
                }
            </style>
        </head>
        <body>
            <div class="container">
                <header class="header">
                    <h1>GitHub Star Monitor</h1>
                    <div class="repo-info">
                        <i class="mdi mdi-github"></i>
                        <span>{{repo_owner}}/{{repo_name}}</span>
                    </div>
                </header>
                
                <div class="controls">
                    <button class="btn btn-primary" onclick="updateStats()">
                        <i class="mdi mdi-refresh"></i>
                        立即刷新
                    </button>
                    <button class="btn btn-outline" onclick="exportData()">
                        <i class="mdi mdi-download"></i>
                        导出数据
                    </button>
                    <button class="btn btn-outline" onclick="toggleSettings()">
                        <i class="mdi mdi-cog"></i>
                        设置
                    </button>
                    <select id="timeRange" onchange="updateTrendChart()">
                        <option value="7">最近7天</option>
                        <option value="30">最近30天</option>
                        <option value="90">最近90天</option>
                    </select>
                </div>
                
                <div class="stats">
                    <div class="stat-card">
                        <div class="label">总Star数</div>
                        <div class="value" id="total-stars">{{total_stars}}</div>
                        <div class="trend up">
                            <i class="mdi mdi-trending-up"></i>
                            <span id="total-trend">+0%</span>
                        </div>
                    </div>
                    <div class="stat-card">
                        <div class="label">今日获得</div>
                        <div class="value" id="today-gained">0</div>
                        <div class="trend up">
                            <i class="mdi mdi-star"></i>
                            <span>新增</span>
                        </div>
                    </div>
                    <div class="stat-card">
                        <div class="label">今日失去</div>
                        <div class="value" id="today-lost">0</div>
                        <div class="trend down">
                            <i class="mdi mdi-star-off"></i>
                            <span>流失</span>
                        </div>
                    </div>
                    <div class="stat-card">
                        <div class="label">最后检查</div>
                        <div class="value" id="last-check">{{last_check_time}}</div>
                        <div class="trend">
                            <i class="mdi mdi-clock-outline"></i>
                            <span>更新时间</span>
                        </div>
                    </div>
                </div>
                
                <div class="card">
                    <h3>Star趋势</h3>
                    <div class="chart-container">
                        <canvas id="trendChart"></canvas>
                    </div>
                </div>
                
                <div class="card">
                    <h3>最近活动</h3>
                    <div class="activity-list" id="activity-list">
                        {% for activity in recent_activities %}
                        <div class="activity-item">
                            <div class="activity-icon">
                                <i class="mdi mdi-star"></i>
                            </div>
                            <div class="activity-content">
                                <div class="activity-message">{{activity.message}}</div>
                                {% if activity.user_info %}
                                <div class="user-info">
                                    <img src="{{activity.user_info.avatar_url}}" alt="{{activity.user_info.login}}" class="user-avatar">
                                    <a href="{{activity.user_info.html_url}}" target="_blank" class="user-name">{{activity.user_info.login}}</a>
                                </div>
                                {% endif %}
                                <div class="activity-time">{{activity.time}}</div>
                            </div>
                        </div>
                        {% endfor %}
                    </div>
                </div>
            </div>
            
            <div id="loading" class="loading" style="display: none;">
                <div class="loading-content">
                    <div class="spinner"></div>
                    <div>数据加载中...</div>
                </div>
            </div>
            
            <div id="settings" class="settings-panel" style="display: none;">
                <div class="settings-content">
                    <div class="settings-header">
                        <h3>设置</h3>
                        <button class="btn btn-outline" onclick="toggleSettings()">
                            <i class="mdi mdi-close"></i>
                        </button>
                    </div>
                    <div class="settings-form">
                        <div class="form-group">
                            <label>
                                <input type="checkbox" id="autoRefresh" checked>
                                自动刷新
                            </label>
                        </div>
                        <div class="form-group">
                            <label>刷新间隔（秒）</label>
                            <input type="number" id="refreshInterval" value="60" min="30">
                        </div>
                        <div class="form-group">
                            <label>
                                <input type="checkbox" id="notifications">
                                启用通知
                            </label>
                        </div>
                        <div class="settings-actions">
                            <button class="btn btn-primary" onclick="saveSettings()">保存</button>
                            <button class="btn btn-outline" onclick="toggleSettings()">取消</button>
                        </div>
                    </div>
                </div>
            </div>
            
            <script>
                let trendChart = null;
                let autoRefreshInterval = null;
                let lastStats = null;
                
                function showLoading() {
                    document.getElementById('loading').style.display = 'grid';
                }
                
                function hideLoading() {
                    document.getElementById('loading').style.display = 'none';
                }
                
                function showError(message) {
                    alert(message);
                }
                
                async function updateStats() {
                    showLoading();
                    try {
                        const response = await fetch('/api/stats');
                        if (!response.ok) {
                            throw new Error('网络请求失败');
                        }
                        const data = await response.json();
                        lastStats = data;
                        
                        document.getElementById('total-stars').textContent = data.total_stars;
                        document.getElementById('today-gained').textContent = data.today_gained;
                        document.getElementById('today-lost').textContent = data.today_lost;
                        document.getElementById('last-check').textContent = data.last_check_time;
                        
                        const trend = ((data.total_gained - data.total_lost) / data.total_stars * 100).toFixed(1);
                        document.getElementById('total-trend').textContent = `${trend > 0 ? '+' : ''}${trend}%`;
                        
                        const activityList = document.getElementById('activity-list');
                        activityList.innerHTML = '';
                        data.recent_activities.forEach(activity => {
                            const item = document.createElement('div');
                            item.className = 'activity-item';
                            
                            let userInfoHtml = '';
                            if (activity.user_info) {
                                userInfoHtml = `
                                    <div class="user-info">
                                        <img src="${activity.user_info.avatar_url}" alt="${activity.user_info.login}" class="user-avatar">
                                        <a href="${activity.user_info.html_url}" target="_blank" class="user-name">${activity.user_info.login}</a>
                                    </div>
                                `;
                            }
                            
                            item.innerHTML = `
                                <div class="activity-icon">
                                    <i class="mdi mdi-star"></i>
                                </div>
                                <div class="activity-content">
                                    <div class="activity-message">${activity.message}</div>
                                    ${userInfoHtml}
                                    <div class="activity-time">${activity.time}</div>
                                </div>
                            `;
                            activityList.appendChild(item);
                        });
                        
                        await updateTrendChart();
                    } catch (error) {
                        showError('数据更新失败: ' + error.message);
                    } finally {
                        hideLoading();
                    }
                }
                
                async function updateTrendChart() {
                    const days = document.getElementById('timeRange').value;
                    try {
                        const response = await fetch(`/api/stats/trend?days=${days}`);
                        const data = await response.json();
                        
                        if (trendChart) {
                            trendChart.destroy();
                        }
                        
                        const ctx = document.getElementById('trendChart').getContext('2d');
                        trendChart = new Chart(ctx, {
                            type: 'line',
                            data: {
                                labels: data.labels,
                                datasets: [
                                    {
                                        label: '获得Star',
                                        data: data.gained,
                                        borderColor: '#22c55e',
                                        backgroundColor: '#22c55e20',
                                        fill: true,
                                        tension: 0.4
                                    },
                                    {
                                        label: '失去Star',
                                        data: data.lost,
                                        borderColor: '#ef4444',
                                        backgroundColor: '#ef444420',
                                        fill: true,
                                        tension: 0.4
                                    }
                                ]
                            },
                            options: {
                                responsive: true,
                                maintainAspectRatio: false,
                                plugins: {
                                    legend: {
                                        position: 'top',
                                    },
                                    tooltip: {
                                        mode: 'index',
                                        intersect: false,
                                    }
                                },
                                scales: {
                                    y: {
                                        beginAtZero: true,
                                        grid: {
                                            color: 'rgba(0, 0, 0, 0.1)'
                                        }
                                    },
                                    x: {
                                        grid: {
                                            display: false
                                        }
                                    }
                                },
                                interaction: {
                                    mode: 'nearest',
                                    axis: 'x',
                                    intersect: false
                                }
                            }
                        });
                    } catch (error) {
                        showError('获取趋势数据失败: ' + error.message);
                    }
                }
                
                async function exportData() {
                    try {
                        const response = await fetch('/api/stats/export');
                        const data = await response.json();
                        const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
                        const url = window.URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = `star-monitor-export-${new Date().toISOString()}.json`;
                        a.click();
                        window.URL.revokeObjectURL(url);
                    } catch (error) {
                        showError('导出数据失败: ' + error.message);
                    }
                }
                
                function toggleSettings() {
                    const panel = document.getElementById('settings');
                    panel.style.display = panel.style.display === 'none' ? 'grid' : 'none';
                }
                
                function saveSettings() {
                    const autoRefresh = document.getElementById('autoRefresh').checked;
                    const refreshInterval = parseInt(document.getElementById('refreshInterval').value);
                    const notifications = document.getElementById('notifications').checked;
                    
                    if (autoRefresh) {
                        if (autoRefreshInterval) {
                            clearInterval(autoRefreshInterval);
                        }
                        autoRefreshInterval = setInterval(updateStats, refreshInterval * 1000);
                    } else {
                        if (autoRefreshInterval) {
                            clearInterval(autoRefreshInterval);
                        }
                    }
                    
                    // 保存设置到localStorage
                    localStorage.setItem('settings', JSON.stringify({
                        autoRefresh,
                        refreshInterval,
                        notifications
                    }));
                    
                    toggleSettings();
                }
                
                // 加载保存的设置
                function loadSettings() {
                    const settings = JSON.parse(localStorage.getItem('settings') || '{}');
                    document.getElementById('autoRefresh').checked = settings.autoRefresh ?? true;
                    document.getElementById('refreshInterval').value = settings.refreshInterval ?? 60;
                    document.getElementById('notifications').checked = settings.notifications ?? false;
                    
                    if (settings.autoRefresh !== false) {
                        autoRefreshInterval = setInterval(updateStats, (settings.refreshInterval || 60) * 1000);
                    }
                }
                
                // 初始化
                document.addEventListener('DOMContentLoaded', () => {
                    loadSettings();
                    updateStats();
                });
            </script>
        </body>
        </html>
        """
        
        # 存储最近的活动
        self.recent_activities = []
        self.max_activities = 50  # 最多保存50条活动记录

        # 添加新的组件
        self.stats = StarStats()
        self.notifications = NotificationSettings()

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

    async def _close_session(self):
        """关闭aiohttp session"""
        if self.session is not None:
            await self.session.close()
            self.session = None

    def should_exit(self) -> bool:
        """检查是否应该退出"""
        return self.graceful_exit.kill_now or self.is_shutting_down

    async def _wait_with_interrupt_check(self, delay: float) -> bool:
        """
        可中断的等待函数

        Args:
            delay: 等待时间（秒）

        Returns:
            bool: True表示正常等待完成，False表示被中断
        """
        try:
            # 将长时间等待分割为短时间片段，以便及时响应中断信号
            check_interval = min(1.0, delay)  # 每秒检查一次或者更短
            elapsed = 0.0

            while elapsed < delay:
                if self.should_exit():
                    return False

                wait_time = min(check_interval, delay - elapsed)
                await asyncio.sleep(wait_time)
                elapsed += wait_time

            return True
        except asyncio.CancelledError:
            self.logger.info("等待被取消")
            return False

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
        if self.should_exit():
            raise asyncio.CancelledError("监控已停止")

        await self._create_session()

        try:
            async with self.session.get(url, params=params) as response:
                # 检查API限制
                if response.status == 403:
                    response_text = await response.text()
                    if 'rate limit' in response_text.lower():
                        reset_time = response.headers.get('X-RateLimit-Reset')
                        if reset_time:
                            # 修复：使用time.time()替代asyncio.get_event_loop().time()
                            current_time = time.time()
                            wait_time = int(reset_time) - int(current_time)

                            # 限制等待时间在合理范围内（1秒到1小时）
                            wait_time = max(1, min(wait_time, 3600))

                            # 添加更详细的日志信息
                            reset_datetime = datetime.fromtimestamp(int(reset_time))
                            self.logger.warning(
                                f"API限制已达到，将等待 {wait_time} 秒后重试 "
                                f"(重置时间: {reset_datetime.strftime('%Y-%m-%d %H:%M:%S')})"
                            )

                            if not await self._wait_with_interrupt_check(wait_time):
                                raise asyncio.CancelledError("等待期间监控被停止")

                            return await self._make_request(url, params)

                response.raise_for_status()
                return await response.json()

        except asyncio.CancelledError:
            raise
        except aiohttp.ClientError as e:
            if not self.should_exit():  # 只在非退出状态下记录错误
                self.logger.error(f"请求失败: {e}")
            raise
        except Exception as e:
            if not self.should_exit():
                self.logger.error(f"未知错误: {e}")
            raise

    async def get_all_stargazers(self) -> List[Dict]:
        """获取所有stargazers"""
        if self.should_exit():
            return []

        stargazers = []
        page = 1
        per_page = 100

        self.logger.info("正在获取stargazers列表...")

        try:
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
            max_concurrent = min(5, 10)  # 降低并发数以避免触发限流
            page = 2

            while not self.should_exit():
                # 创建一批并发任务
                batch_tasks = []
                for i in range(max_concurrent):
                    current_page = page + i
                    params = {'page': current_page, 'per_page': per_page}
                    task = self._make_request(self.stargazers_url, params)
                    batch_tasks.append(task)

                # 执行当前批次
                try:
                    batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    break

                has_data = False
                for result in batch_results:
                    if isinstance(result, Exception):
                        if not isinstance(result, asyncio.CancelledError):
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

                # 增加请求间隔以避免触发限流
                if not await self._wait_with_interrupt_check(0.5):
                    break

        except asyncio.CancelledError:
            self.logger.info("获取stargazers被中断")

        self.logger.info(f"获取完成，共 {len(stargazers)} 个stargazers")
        return stargazers

    async def get_repo_info(self) -> Dict:
        """获取仓库基本信息"""
        return await self._make_request(self.repo_url)

    async def initialize_stargazers(self):
        """初始化stargazers列表"""
        if self.should_exit():
            return

        try:
            self.logger.info("正在初始化...")

            # 尝试加载历史状态
            has_history = self.load_state()

            # 获取当前仓库信息
            repo_info = await self.get_repo_info()
            current_total_stars = repo_info['stargazers_count']

            if has_history and not self.should_exit():
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
                    self.logger.info(
                        f"stars数量变化较大 ({self.total_stars} -> {current_total_stars})，重新获取完整列表")

            if self.should_exit():
                return

            # 获取完整的stargazers列表
            stargazers = await self.get_all_stargazers()

            if self.should_exit():
                return

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

        except asyncio.CancelledError:
            self.logger.info("初始化被中断")
        except Exception as e:
            if not self.should_exit():
                self.logger.error(f"初始化失败: {e}")
            raise

    async def check_star_changes(self):
        """检查star变化"""
        if self.should_exit():
            return

        try:
            # 并发获取当前的stargazers和仓库信息
            stargazers_task = self.get_all_stargazers()
            repo_info_task = self.get_repo_info()

            current_stargazers_list, repo_info = await asyncio.gather(
                stargazers_task, repo_info_task, return_exceptions=True
            )

            # 检查是否有异常
            if isinstance(current_stargazers_list, Exception):
                if not isinstance(current_stargazers_list, asyncio.CancelledError):
                    self.logger.error(f"获取stargazers失败: {current_stargazers_list}")
                return

            if isinstance(repo_info, Exception):
                if not isinstance(repo_info, asyncio.CancelledError):
                    self.logger.error(f"获取仓库信息失败: {repo_info}")
                return

            if self.should_exit():
                return

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
                            'id': user_info['id'],
                            'login': username
                        }

                        self.logger.info(f"⭐ 新增Star: {username}")
                        self.logger.info(f"   用户链接: {user_info['html_url']}")
                        self.logger.info(f"   用户头像: {user_info['avatar_url']}")
                        if user_info.get('name'):
                            self.logger.info(f"   用户名称: {user_info['name']}")
                            
                        # 添加活动记录（包含用户信息）
                        self.add_activity(f"⭐ 新增Star: {username}", self.stargazers_info[username])

            # 处理取消的stars
            if removed_stars:
                for username in removed_stars:
                    # 尝试从历史信息中获取用户详情
                    user_info = self.stargazers_info.get(username, {})
                    if not user_info:
                        user_info = {
                            'html_url': f'https://github.com/{username}',
                            'avatar_url': f'https://github.com/{username}.png',
                            'login': username
                        }

                    self.logger.info(f"💔 取消Star: {username}")
                    self.logger.info(f"   用户链接: {user_info.get('html_url', f'https://github.com/{username}')}")
                    if user_info.get('avatar_url'):
                        self.logger.info(f"   用户头像: {user_info['avatar_url']}")
                        
                    # 添加活动记录（包含用户信息）
                    self.add_activity(f"💔 取消Star: {username}", user_info)

            # 更新当前stargazers集合
            self.current_stargazers = new_stargazers_set

            # 更新用户信息字典，添加新用户
            for star in current_stargazers_list:
                if star['login'] not in self.stargazers_info:
                    self.stargazers_info[star['login']] = {
                        'html_url': star['html_url'],
                        'avatar_url': star['avatar_url'],
                        'id': star['id'],
                        'login': star['login']
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

        except asyncio.CancelledError:
            self.logger.info("检查被中断")
        except Exception as e:
            if not self.should_exit():
                self.logger.error(f"检查star变化时出错: {e}")

    async def cleanup(self):
        """清理资源"""
        self.logger.info("正在清理资源...")
        self.is_shutting_down = True

        try:
            # 取消所有运行中的任务
            if self.running_tasks:
                self.logger.info(f"取消 {len(self.running_tasks)} 个运行中的任务")
                for task in self.running_tasks:
                    if not task.done():
                        task.cancel()

                # 等待任务完成取消
                if self.running_tasks:
                    await asyncio.gather(*self.running_tasks, return_exceptions=True)

            # 关闭网络连接
            await self._close_session()

            # 最后保存一次状态
            self.save_state()
            self.logger.info(f"状态已保存到: {self.state_file}")

        except Exception as e:
            self.logger.error(f"清理资源时出错: {e}")
        finally:
            self.logger.info("资源清理完成")

    def add_activity(self, message: str, user_info: Dict = None):
        """添加活动记录（同时更新统计数据）"""
        activity = {
            'message': message,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'user_info': user_info
        }
        self.recent_activities.insert(0, activity)
        self.recent_activities = self.recent_activities[:self.max_activities]
        
        # 更新统计数据
        if '新增Star' in message:
            self.stats.update('gain')
        elif '取消Star' in message:
            self.stats.update('lost')

    @lru_cache(maxsize=100)
    def get_cached_stats(self, time_range: str) -> Dict:
        """获取缓存的统计数据"""
        return self.stats.get_trend_data(int(time_range))

    async def handle_trend(self, request):
        """处理趋势数据请求"""
        days = request.query.get('days', '7')
        data = self.get_cached_stats(days)
        return web.json_response(data)

    async def handle_export(self, request):
        """处理数据导出请求"""
        data = {
            'repository': self.repo_full_name,
            'total_stars': self.total_stars,
            'stats': {
                'daily': dict(self.stats.daily_stats),
                'hourly': dict(self.stats.hourly_stats),
                'total_gained': self.stats.total_gained,
                'total_lost': self.stats.total_lost
            },
            'recent_activities': self.recent_activities,
            'export_time': datetime.now().isoformat()
        }
        return web.json_response(data)

    async def handle_activities(self, request):
        """处理活动列表请求（支持分页）"""
        page = int(request.query.get('page', '1'))
        per_page = int(request.query.get('per_page', '20'))
        start = (page - 1) * per_page
        end = start + per_page
        
        return web.json_response({
            'activities': self.recent_activities[start:end],
            'total': len(self.recent_activities),
            'page': page,
            'per_page': per_page
        })

    async def handle_settings(self, request):
        """处理设置更新请求"""
        try:
            data = await request.json()
            # 更新通知设置
            self.notifications.email_enabled = data.get('email_enabled', False)
            self.notifications.email_address = data.get('email_address')
            self.notifications.webhook_url = data.get('webhook_url')
            self.notifications.notify_on_milestone = data.get('notify_on_milestone', True)
            return web.json_response({'status': 'success'})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=400)

    async def handle_index(self, request):
        """处理首页请求"""
        template = jinja2.Template(self.template)
        html = template.render(
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
            total_stars=self.total_stars,
            last_check_time=self.last_check_time or "未开始检查",
            recent_activities=self.recent_activities
        )
        return web.Response(text=html, content_type='text/html')

    async def handle_stats(self, request):
        """处理API统计数据请求"""
        today = datetime.now().strftime('%Y-%m-%d')
        return web.json_response({
            'total_stars': self.total_stars,
            'last_check_time': self.last_check_time or "未开始检查",
            'recent_activities': self.recent_activities,
            'today_gained': self.stats.daily_stats[today]['gained'],
            'today_lost': self.stats.daily_stats[today]['lost'],
            'total_gained': self.stats.total_gained,
            'total_lost': self.stats.total_lost
        })

    async def start_web_server(self):
        """启动Web服务器"""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, 'localhost', self.web_port)
        await site.start()
        self.logger.info(f"Web界面已启动: http://localhost:{self.web_port}")

    async def start_monitoring(self):
        """开始监控"""
        self.logger.info(f"开始监控仓库: {self.repo_full_name}")
        self.logger.info(f"检查间隔: {self.check_interval} 秒")
        self.logger.info(f"状态文件: {self.state_file}")
        self.logger.info("提示: 使用 Ctrl+C 或发送 SIGTERM 信号来优雅停止监控")
        self.logger.info("=" * 50)

        self.is_running = True

        try:
            # 启动Web服务器
            await self.start_web_server()
            
            # 初始化
            await self.initialize_stargazers()

            if self.should_exit():
                return

            self.logger.info("监控已启动，按 Ctrl+C 优雅退出")
            self.add_activity("监控已启动")

            # 开始循环监控
            while not self.should_exit():
                if not await self._wait_with_interrupt_check(self.check_interval):
                    break

                if self.should_exit():
                    break

                self.logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 检查star变化...")
                await self.check_star_changes()

        except asyncio.CancelledError:
            self.logger.info("监控任务被取消")
            self.add_activity("监控被取消")
        except Exception as e:
            self.logger.error(f"监控过程中出错: {e}")
            self.add_activity(f"监控出错: {e}")
            raise
        finally:
            self.is_running = False
            self.logger.info("监控已停止")
            self.add_activity("监控已停止")
            # 确保清理资源
            await self.cleanup()


async def main():
    parser = argparse.ArgumentParser(description='GitHub Star 监控器（异步版本，优雅退出）')
    parser.add_argument('repo', help='仓库名称，格式: owner/repo')
    parser.add_argument('--token', '-t', help='GitHub Personal Access Token')
    parser.add_argument('--interval', '-i', type=int, default=60,
                        help='检查间隔（秒），默认60秒')
    parser.add_argument('--log-file', '-l', help='日志文件路径')
    parser.add_argument('--state-file', '-s', help='状态文件路径，默认为 star_monitor_owner_repo.json')
    parser.add_argument('--port', '-p', type=int, default=8080,
                        help='Web界面端口，默认8080')

    args = parser.parse_args()

    # 解析仓库名称
    try:
        repo_owner, repo_name = args.repo.split('/')
    except ValueError:
        print("错误: 仓库名称格式应为 'owner/repo'")
        return 1

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
        state_file=args.state_file,
        web_port=args.port
    )

    try:
        # 开始监控
        await monitor.start_monitoring()
        return 0
    except KeyboardInterrupt:
        print("\n收到键盘中断，正在退出...")
        return 0
    except Exception as e:
        print(f"监控过程中发生未处理的错误: {e}")
        return 1
    finally:
        print("程序退出完成")


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n程序被中断")
        sys.exit(0)
    except Exception as e:
        print(f"程序启动失败: {e}")
        sys.exit(1)
