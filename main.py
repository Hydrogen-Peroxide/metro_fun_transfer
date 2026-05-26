"""
地铁·趣味换乘插件
支持广州地铁、深圳地铁等多数据集切换
说出换乘站名即可参与游戏
"""

import re
from typing import Set, Dict, Optional
from datetime import datetime

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

from .stations import list_datasets, get_dataset


class GuangzhouMetroTransferGame(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        self.dataset_name: str = ""
        self.all_stations: Set[str] = set()
        self.current_lines: Dict[str, list] = {}

        self.remaining_stations: Set[str] = set()
        self.found_stations: Dict[str, Dict] = {}
        self.list_message_id: Optional[str] = None
        self.list_origin: Optional[str] = None
        self.enabled = False

        self.station_pattern = None
        self.station_aliases = {
            "建设六马路": "建设六马路·淘金",
            "淘金": "建设六马路·淘金",
        }

        self._load_dataset("广州地铁")

    def _load_dataset(self, name: str):
        data = get_dataset(name)
        if not data:
            return

        self.dataset_name = name
        self.all_stations = data["stations"].copy()
        self.current_lines = data["stations_by_line"]
        self._rebuild_pattern()

    def _rebuild_pattern(self):
        all_pattern_names = list(self.all_stations) + list(self.station_aliases.keys())
        sorted_stations = sorted(all_pattern_names, key=len, reverse=True)
        escaped_stations = [re.escape(name) for name in sorted_stations]
        pattern_str = "|".join(escaped_stations)
        self.station_pattern = re.compile(f"({pattern_str})")

    @filter.command("metro_start")
    async def start_command(self, event: AstrMessageEvent):
        if self.enabled:
            yield event.plain_result("🚇 游戏已经在进行中！")
            return

        self.enabled = True
        self.remaining_stations = self.all_stations.copy()
        self.found_stations.clear()

        welcome_msg = (
            f"🚇 广州地铁换乘游戏开始！\n"
            f"📁 当前数据集：{self.dataset_name}\n\n"
            "游戏规则：\n"
            "1. 我会先发布所有换乘站的完整列表\n"
            "2. 当您发送包含某个换乘站的消息时，这个站将从列表中删除\n"
            "3. 目标是清空所有换乘站！\n\n"
            "输入 !metro_status 查看当前状态\n"
            "输入 !metro_rank 查看排行榜\n"
            "输入 !metro_stop 停止游戏"
        )

        yield event.plain_result(welcome_msg)

        if not self.remaining_stations:
            list_msg = "🎉 恭喜！所有换乘站都已被找到！游戏结束！"
        else:
            sorted_stations = sorted(self.remaining_stations)
            stations_str = "、".join(sorted_stations)
            count = len(self.remaining_stations)
            list_msg = f"🚇 剩余换乘站列表（共{count}个）:\n\n{stations_str}"

        yield event.plain_result(list_msg)

    @filter.command("metro_stop")
    async def stop_command(self, event: AstrMessageEvent):
        if not self.enabled:
            yield event.plain_result("🚫 游戏尚未开始！")
            return

        self.enabled = False

        total = len(self.all_stations)
        found = len(self.found_stations)
        remaining = total - found

        stats_msg = (
            f"🛑 游戏已停止！\n\n"
            f"📊 游戏统计：\n"
            f"总换乘站数: {total}\n"
            f"已找到: {found}\n"
            f"剩余: {remaining}\n\n"
        )

        if found > 0:
            stats_msg += "🏆 找到换乘站的玩家：\n"
            for station, info in self.found_stations.items():
                user_info = f"用户{info.get('user_id', '')}"
                if 'username' in info:
                    user_info = info['username']
                time_str = info.get('time', '').strftime("%H:%M:%S") if info.get('time') else ""
                stats_msg += f"  {station} - {user_info} {time_str}\n"

        yield event.plain_result(stats_msg)

        self.remaining_stations.clear()
        self.found_stations.clear()
        self.list_message_id = None
        self.list_origin = None

    @filter.command("metro_status")
    async def status_command(self, event: AstrMessageEvent):
        if not self.enabled:
            yield event.plain_result("🚫 游戏未在进行中，使用 !metro_start 开始游戏")
            return

        total = len(self.all_stations)
        found = len(self.found_stations)
        remaining = len(self.remaining_stations)
        progress = round(found / total * 100, 1) if total > 0 else 0

        status_msg = (
            f"🚇 游戏进行中！\n"
            f"📁 数据集：{self.dataset_name}\n\n"
            f"📊 进度：{progress}%\n"
            f"已找到: {found}/{total}\n"
            f"剩余: {remaining}\n\n"
            f"发送任意包含换乘站的消息来找到它们！\n"
            f"使用 !metro_rank 查看排行榜"
        )

        yield event.plain_result(status_msg)

    @filter.command("metro_rank")
    async def rank_command(self, event: AstrMessageEvent):
        if not self.found_stations:
            yield event.plain_result("📊 暂无找到的车站记录")
            return

        user_stats = {}
        for station, info in self.found_stations.items():
            user_id = info.get('user_id', 'unknown')
            username = info.get('username', f'用户{user_id}')
            if user_id not in user_stats:
                user_stats[user_id] = {'username': username, 'count': 0, 'stations': []}
            user_stats[user_id]['count'] += 1
            user_stats[user_id]['stations'].append(station)

        sorted_users = sorted(user_stats.items(), key=lambda x: x[1]['count'], reverse=True)

        rank_msg = "🏆 地铁换乘游戏排行榜：\n\n"
        for idx, (user_id, data) in enumerate(sorted_users, 1):
            medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else ("🥉" if idx == 3 else f"{idx}."))
            stations_str = "、".join(data['stations'][:5])
            if len(data['stations']) > 5:
                stations_str += f" 等{len(data['stations'])}个"
            rank_msg += f"{medal} {data['username']}: {data['count']}个\n"

        yield event.plain_result(rank_msg)

    @filter.command("metro_list")
    async def list_command(self, event: AstrMessageEvent):
        if not self.enabled:
            yield event.plain_result("🚫 游戏未在进行中")
            return

        if not self.remaining_stations:
            yield event.plain_result("🎉 所有车站都已被找到！太厉害了！")
            return

        sorted_stations = sorted(self.remaining_stations)
        stations_str = "、".join(sorted_stations)
        count = len(self.remaining_stations)
        list_msg = f"🚇 剩余换乘站列表（共{count}个）:\n\n{stations_str}"

        yield event.plain_result(list_msg)

    @filter.command("metro_found")
    async def found_command(self, event: AstrMessageEvent):
        if not self.found_stations:
            yield event.plain_result("🔍 还没有找到任何车站")
            return

        found_msg = "✅ 已找到的车站：\n\n"
        stations_by_user = {}

        for station, info in self.found_stations.items():
            user_id = info.get('user_id', 'unknown')
            if user_id not in stations_by_user:
                stations_by_user[user_id] = []
            stations_by_user[user_id].append(station)

        for user_id, stations in stations_by_user.items():
            username = self.found_stations.get(stations[0], {}).get('username', f'用户{user_id}')
            stations_str = "、".join(stations)
            found_msg += f"👤 {username}:\n{stations_str}\n\n"

        yield event.plain_result(found_msg)

    @filter.command("metro_switch")
    async def switch_command(self, event: AstrMessageEvent):
        if self.enabled:
            yield event.plain_result("🚫 请先使用 !metro_stop 停止当前游戏再切换数据集")
            return

        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            datasets = list_datasets()
            yield event.plain_result(
                "📁 可用数据集：\n" +
                "\n".join(f"  {d}" for d in datasets) +
                "\n\n使用方法：!metro_switch 数据集名称"
            )
            return

        name = parts[1].strip()
        data = get_dataset(name)
        if not data:
            yield event.plain_result(f"❌ 未找到数据集「{name}」，使用 !metro_datasets 查看可用数据集")
            return

        self._load_dataset(name)
        yield event.plain_result(f"✅ 已切换到数据集「{name}」\n共 {len(self.all_stations)} 个换乘站")

    @filter.command("metro_datasets")
    async def datasets_command(self, event: AstrMessageEvent):
        datasets = list_datasets()
        msg = "📁 可用数据集：\n\n"
        for d in datasets:
            data = get_dataset(d)
            count = len(data["stations"]) if data else 0
            current = " ← 当前" if d == self.dataset_name else ""
            msg += f"  {d}（{count}站）{current}\n"
        msg += "\n使用 !metro_switch 数据集名称 切换"
        yield event.plain_result(msg)

    @filter.command("metro_help")
    async def help_command(self, event: AstrMessageEvent):
        help_text = f"""
🚇 广州地铁换乘游戏 - 帮助手册
📁 当前数据集：{self.dataset_name}

命令列表：
!metro_start     - 开始游戏
!metro_stop      - 停止游戏
!metro_status    - 查看当前状态
!metro_list      - 查看剩余车站列表
!metro_found     - 查看已找到的车站
!metro_rank      - 查看排行榜
!metro_datasets  - 查看可用数据集
!metro_switch <名> - 切换数据集
!metro_help      - 显示此帮助信息

游戏规则：
1. 游戏开始后，我会发布所有换乘站的完整列表
2. 当您发送的消息中包含列表中的换乘站时，该站将从列表中删除
3. 目标是清空所有换乘站！
4. 找到车站的玩家会被记录在排行榜中

小贴士：
- 发送完整的车站名称
- 可以一次发送包含多个车站的消息
- 游戏可以多人一起玩
        """

        yield event.plain_result(help_text)

    def _get_line_hints(self, station: str) -> str:
        found_lines = []
        for line_name, line_stations in self.current_lines.items():
            if station not in line_stations:
                continue
            unfound = [s for s in line_stations if s in self.remaining_stations and s != station]
            if unfound:
                found_lines.append(f"{line_name}：{'、'.join(unfound)}")
        if not found_lines:
            return ""
        return "\n" + "\n".join(found_lines)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        if not self.enabled:
            return

        msg = event.message_str.strip()
        if not msg:
            return

        matches = list(self.station_pattern.finditer(msg))
        if not matches:
            return

        found_in_msg = []
        for match in matches:
            station = self.station_aliases.get(match.group(1), match.group(1))
            if station in self.remaining_stations:
                self.remaining_stations.remove(station)

                user_id = event.get_sender_id()
                username = event.get_sender_name() or f"用户{user_id}"
                self.found_stations[station] = {
                    'user_id': user_id,
                    'username': username,
                    'time': datetime.now()
                }

                found_in_msg.append(station)

        if found_in_msg:
            found_count = len(found_in_msg)
            station_list = "、".join(found_in_msg)

            if found_count == 1:
                feedback = f"✅ 您现在在{station_list}"
                hint = self._get_line_hints(found_in_msg[0])
                if hint:
                    feedback += f"\n\n🚇 同线网上还未使用的换乘站：{hint}"
            else:
                feedback = f"🎉 您现在在{station_list}"
                for station in found_in_msg:
                    hint = self._get_line_hints(station)
                    if hint:
                        feedback += f"\n\n🚇 同线网上还未使用的换乘站（{station}）：{hint}"

            feedback += f"\n\n剩余车站数：{len(self.remaining_stations)}"

            if not self.remaining_stations:
                feedback += "\n\n🎊 🎊 🎊 恭喜！所有换乘站都已被找到！游戏胜利！"
                self.enabled = False

                yield event.plain_result(feedback)

                total = len(self.all_stations)
                found = len(self.found_stations)
                win_msg = (
                    f"🏆 游戏结束！最终统计：\n\n"
                    f"总换乘站数: {total}\n"
                    f"已找到: {found}\n"
                )

                if found > 0:
                    user_stats = {}
                    for station, info in self.found_stations.items():
                        user_id = info.get('user_id', 'unknown')
                        username = info.get('username', f'用户{user_id}')
                        if user_id not in user_stats:
                            user_stats[user_id] = {'username': username, 'count': 0}
                        user_stats[user_id]['count'] += 1

                    sorted_users = sorted(user_stats.items(), key=lambda x: x[1]['count'], reverse=True)
                    win_msg += "\n🏅 排行榜：\n"
                    for idx, (user_id, data) in enumerate(sorted_users[:3], 1):
                        medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else "🥉")
                        win_msg += f"{medal} {data['username']}: {data['count']}个\n"

                yield event.plain_result(win_msg)

                self.remaining_stations.clear()
                self.found_stations.clear()
                logger.info(f"游戏结束！用户找到了所有车站")
                return

            yield event.plain_result(feedback)

            logger.info(f"用户 {event.get_sender_id()} 找到了车站: {station_list}")