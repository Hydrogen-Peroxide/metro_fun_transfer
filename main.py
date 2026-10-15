"""
地铁游园插件
基于2027年广州地铁线网图的双人联机对战
BP选区 + 必须换乘 + 打击车站 + 有轨/APM卡牌系统
"""

import re
from typing import Set, Dict, List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

from .stations import list_datasets, get_dataset


class MetroGardenGame(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        # 数据
        self.dataset_name: str = ""
        self.all_stations: Set[str] = set()
        self.current_lines: Dict[str, list] = {}
        self.card_lines: Dict[str, list] = {}
        self.districts: Dict[str, list] = {}

        # 游戏状态
        self.enabled = False
        self.game_phase = 'lobby'  # 'lobby', 'ban', 'playing'
        self.setup_step = 0

        # 玩家
        self.duel_waiting: List[tuple] = []
        self.duel_players: List[tuple] = []
        self.current_turn_idx = 0

        # 目的地
        self.player_dest: Dict[str, str] = {}
        self.player_dest_lines: Dict[str, List[str]] = {}
        self.dead_lines: Set[str] = set()  # 双方均选3线+目的地且线路共用时的死亡线路
        self.strike_station: str = ""  # 触发打击的车站

        # 棋子位置（双方共用）
        self.current_station: str = ""
        self.current_line: str = ""

        # 消耗
        self.consumed_stations: Set[str] = set()
        self.consumed_cards: Set[str] = set()

        # ban状态
        self.banned_lines: Set[str] = set()
        self.banned_line_pairs: Set[tuple] = set()  # 全局线路对ban（飞站）

        # 行政区
        self.selected_district: str = ""

        # 打击状态
        self.strike_line: str = ""
        self.strike_attacker: str = ""

        # 卡牌连续使用限制
        self.last_action_was_card: bool = False

        # 匹配用
        self.station_pattern = None
        self.station_aliases = {
            "建设六马路": "建设六马路·淘金",
            "淘金": "建设六马路·淘金",
        }

        self._load_dataset("广州地铁2027")

    # ==================== 数据加载 ====================

    def _load_dataset(self, name: str):
        data = get_dataset(name)
        if not data:
            return
        self.dataset_name = name
        self.all_stations = data["stations"].copy()
        self.current_lines = data["stations_by_line"]
        self.card_lines = data.get("card_lines", {})
        self.districts = data.get("stations_by_district", {})
        self._rebuild_pattern()

    def _rebuild_pattern(self):
        all_names = list(self.all_stations) + list(self.station_aliases.keys())
        sorted_names = sorted(all_names, key=len, reverse=True)
        escaped = [re.escape(n) for n in sorted_names]
        self.station_pattern = re.compile("(" + "|".join(escaped) + ")")

    def _reset_game(self):
        self.enabled = False
        self.game_phase = 'lobby'
        self.setup_step = 0
        self.duel_players.clear()
        self.duel_waiting.clear()
        self.current_turn_idx = 0
        self.player_dest.clear()
        self.player_dest_lines.clear()
        self.current_station = ""
        self.current_line = ""
        self.consumed_stations.clear()
        self.consumed_cards.clear()
        self.banned_lines.clear()
        self.banned_line_pairs.clear()
        self.selected_district = ""
        self.strike_line = ""
        self.strike_attacker = ""
        self.strike_station = ""  # 触发打击的车站，被打击方只能沿其非打击线路机动
        self.dead_lines = set()  # 双方均选3线+目的地且线路共用时的死亡线路
        self.last_action_was_card = False

    # ==================== 站点查询 ====================

    def _get_station_lines(self, station: str) -> List[str]:
        """获取车站的有效线路（排除被ban线路 + 飞站效果）"""
        lines = []
        for line_name, line_stations in self.current_lines.items():
            if line_name in self.banned_lines:
                continue
            if station in line_stations:
                lines.append(line_name)
        # 飞站效果：如果车站同时在被ban线路对的两条线上，移除这两条线
        to_remove = set()
        for pair in self.banned_line_pairs:
            l1, l2 = pair
            if l1 in lines and l2 in lines:
                to_remove.add(l1)
                to_remove.add(l2)
        lines = [l for l in lines if l not in to_remove]
        return lines

    def _is_station_available_simple(self, station: str) -> bool:
        """车站是否可用：至少2条有效线路"""
        return len(self._get_station_lines(station)) >= 2

    def _stations_share_line(self, s1: str, s2: str, exclude_line: str = "") -> List[str]:
        """返回连接两站的有效线路列表（排除exclude_line和被ban线路）"""
        s1_lines = set(self._get_station_lines(s1))
        s2_lines = set(self._get_station_lines(s2))
        shared = []
        for line_name in s1_lines & s2_lines:
            if line_name == exclude_line:
                continue
            shared.append(line_name)
        return shared

    # ==================== 游戏逻辑 ====================

    def _get_legal_moves(self) -> List[str]:
        """获取当前玩家可移动的合法车站列表"""
        if not self.current_station:
            return []

        current_player_id = self.duel_players[self.current_turn_idx][0]
        current_player_dest = self.player_dest.get(current_player_id, "")

        other_idx = 1 - self.current_turn_idx
        other_id = self.duel_players[other_idx][0]
        other_dest = self.player_dest.get(other_id, "")

        moves = []
        # 被打击时：可用线路 = 打击车站的线路 − 打击线
        strike_avail = set()
        if self.strike_line and self.strike_station:
            strike_avail = set(self.current_lines.get(self.strike_station, [])) - {self.strike_line}
        for station in self.all_stations:
            if station == self.current_station:
                continue
            if not self._is_station_available_simple(station):
                continue
            if self.strike_line:
                # 被打击：只能前往打击车站其他线路上的换乘站
                station_lines = set(
                    l for l, sts in self.current_lines.items() if station in sts
                )
                if not (station_lines & strike_avail):
                    continue
            else:
                # 正常状态：必须换乘（不可停留在当前线路）
                shared = self._stations_share_line(self.current_station, station, self.current_line)
                if not shared and self.current_line:
                    continue
            # 死亡线路车站不可前往（进入即判负）
            if self.dead_lines and any(
                station in self.current_lines.get(dl, []) for dl in self.dead_lines
            ):
                continue
            if station in self.consumed_stations:
                if station != current_player_dest and station != other_dest:
                    continue
            moves.append(station)

        return sorted(moves)

    def _get_move_hints(self) -> str:
        moves = self._get_legal_moves()
        if not moves:
            return ""
        show = moves[:10]
        result = "、".join(show)
        if len(moves) > 10:
            result += f" 等{len(moves)}站"
        return result

    def _check_strike(self, station: str, player_id: str) -> List[str]:
        """检查车站是否含当前玩家目的地线路，返回打击线路列表"""
        dest_lines = self.player_dest_lines.get(player_id, [])
        station_lines = self._get_station_lines(station)
        return [l for l in dest_lines if l in station_lines]

    def _announce_winner(self, winner_idx: int, reason: str) -> str:
        winner_name = self.duel_players[winner_idx][1]
        loser_name = self.duel_players[1 - winner_idx][1]
        w_id = self.duel_players[winner_idx][0]
        l_id = self.duel_players[1 - winner_idx][0]
        return (
            f"🏆 比赛结束！\n\n"
            f"胜者：{winner_name}\n"
            f"原因：{reason}\n\n"
            f"📊 对战统计：\n"
            f"👤 {winner_name}（目的地：{self.player_dest.get(w_id, '?')}）\n"
            f"👤 {loser_name}（目的地：{self.player_dest.get(l_id, '?')}）\n"
            f"已消耗车站：{len(self.consumed_stations)}\n"
            f"已使用卡牌：{len(self.consumed_cards)}"
        )

    def _parse_arg(self, event: AstrMessageEvent) -> str:
        """解析独立命令的参数：/metro_xxx <参数>"""
        parts = event.message_str.strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    # ==================== 匹配命令 ====================

    @filter.command("metro_join")
    async def cmd_join(self, event: AstrMessageEvent):
        if self.enabled:
            yield event.plain_result("🚫 游戏已在进行中")
            return
        user_id = event.get_sender_id()
        username = event.get_sender_name() or f"用户{user_id}"
        for pid, _ in self.duel_waiting:
            if pid == user_id:
                yield event.plain_result(f"👤 {username} 已在等待队列中")
                return
        self.duel_waiting.append((user_id, username))
        msg = f"👤 {username} 已加入等待队列！（{len(self.duel_waiting)}/2）"
        if len(self.duel_waiting) == 2:
            p1 = self.duel_waiting[0][1]
            p2 = self.duel_waiting[1][1]
            msg += f"\n\n✅ 已满两人！\n👤 玩家A: {p1}\n👤 玩家B: {p2}\n\n发送 /metro_start 开始BP阶段！"
        yield event.plain_result(msg)

    @filter.command("metro_leave")
    async def cmd_leave(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        username = event.get_sender_name() or f"用户{user_id}"
        before = len(self.duel_waiting)
        self.duel_waiting = [(pid, pn) for pid, pn in self.duel_waiting if pid != user_id]
        if before != len(self.duel_waiting):
            yield event.plain_result(f"👋 {username} 已离开等待队列（{len(self.duel_waiting)}/2）")
        else:
            yield event.plain_result(f"👤 {username} 不在等待队列中")

    @filter.command("metro_start")
    async def cmd_start(self, event: AstrMessageEvent):
        if self.enabled:
            yield event.plain_result("🚫 游戏已在进行中！")
            return
        if len(self.duel_waiting) < 2:
            yield event.plain_result(f"🚫 等待队列中只有 {len(self.duel_waiting)} 人，需要2人")
            return
        self.duel_players = self.duel_waiting.copy()
        self.duel_waiting.clear()
        self.enabled = True
        self.game_phase = 'ban'
        self.setup_step = 0
        p1_name = self.duel_players[0][1]
        p2_name = self.duel_players[1][1]
        yield event.plain_result(
            f"🌿 地铁游园 - BP阶段\n\n"
            f"👤 玩家A: {p1_name}\n"
            f"👤 玩家B: {p2_name}\n\n"
            f"📁 数据集：{self.dataset_name}\n"
            f"共 {len(self.all_stations)} 个换乘站\n"
            f"有轨/APM卡牌：{', '.join(self.card_lines.keys())}\n\n"
            f"第一步：玩家A ban一条线路\n"
            f"使用：/metro_banline <线路名>（或 skip 跳过）"
        )

    # ==================== BP阶段命令 ====================

    @filter.command("metro_banline")
    async def cmd_banline(self, event: AstrMessageEvent):
        if self.game_phase != 'ban':
            yield event.plain_result("🚫 当前不在BP阶段")
            return
        if self.setup_step not in (0, 2):
            yield event.plain_result("⏳ 当前不在ban线阶段")
            return
        user_id = event.get_sender_id()
        expected = self.duel_players[self.setup_step // 2][0]
        if user_id != expected:
            yield event.plain_result("⏳ 当前不是你的回合")
            return
        player_name = self.duel_players[self.setup_step // 2][1]
        arg = self._parse_arg(event)

        if arg.lower() == "skip":
            yield event.plain_result(f"⏩ {player_name} 跳过ban线")
        else:
            if not arg:
                yield event.plain_result("请指定要ban的线路名，例如：/metro_banline 3号线")
                return
            if arg not in self.current_lines:
                yield event.plain_result(f"❌ 线路「{arg}」不存在")
                return
            if arg in self.banned_lines:
                yield event.plain_result(f"❌ 线路「{arg}」已被ban")
                return
            self.banned_lines.add(arg)
            yield event.plain_result(f"🛑 {player_name} ban了线路「{arg}」")

        self.setup_step += 1
        if self.setup_step == 1:
            yield event.plain_result(
                f"📌 第二步：玩家A ban一组换乘关系（飞站）\n"
                f"格式：/metro_bantransfer <线1> <线2>\n"
                f"例：/metro_bantransfer 5号线 13号线 → 所有5∩13交汇车站飞站\n"
                f"（或 skip 跳过）"
            )
        elif self.setup_step == 3:
            yield event.plain_result(
                f"📌 第四步：玩家B ban一组换乘关系（飞站）\n"
                f"格式：/metro_bantransfer <线1> <线2>\n"
                f"（或 skip 跳过）"
            )

    @filter.command("metro_bantransfer")
    async def cmd_bantransfer(self, event: AstrMessageEvent):
        if self.game_phase != 'ban':
            yield event.plain_result("🚫 当前不在BP阶段")
            return
        if self.setup_step not in (1, 3):
            yield event.plain_result("⏳ 当前不在ban站阶段")
            return
        user_id = event.get_sender_id()
        expected = self.duel_players[self.setup_step // 2][0]
        if user_id != expected:
            yield event.plain_result("⏳ 当前不是你的回合")
            return
        player_name = self.duel_players[self.setup_step // 2][1]
        arg = self._parse_arg(event)

        if arg.lower() == "skip":
            yield event.plain_result(f"⏩ {player_name} 跳过ban站")
        else:
            ban_parts = arg.strip().split()
            if len(ban_parts) < 2:
                yield event.plain_result("请指定两条线路，例如：/metro_bantransfer 5号线 13号线")
                return
            line1, line2 = ban_parts[0], ban_parts[1]
            if line1 not in self.current_lines or line2 not in self.current_lines:
                yield event.plain_result("❌ 指定的线路不存在")
                return
            if line1 == line2:
                yield event.plain_result("❌ 不能ban同一条线路")
                return
            pair = tuple(sorted([line1, line2]))
            if pair in self.banned_line_pairs:
                yield event.plain_result(f"❌ 线路对「{line1}∩{line2}」已被ban")
                return
            self.banned_line_pairs.add(pair)

            affected = []
            for s in self.all_stations:
                raw = [ln for ln, ls in self.current_lines.items() if s in ls]
                if line1 in raw and line2 in raw:
                    affected.append(s)
            newly_disabled = [s for s in affected if not self._is_station_available_simple(s)]
            msg = f"🛑 {player_name} ban了线路对「{line1}∩{line2}」"
            if affected:
                msg += f"\n受影响车站：{', '.join(affected)}"
            if newly_disabled:
                msg += f"\n❌ 降为不可用：{', '.join(newly_disabled)}"
            yield event.plain_result(msg)

        self.setup_step += 1
        if self.setup_step == 2:
            yield event.plain_result(f"📌 第三步：玩家B ban一条线路\n使用：/metro_banline <线路名>（或skip跳过）")
        elif self.setup_step == 4:
            p2_name = self.duel_players[1][1]
            banned_lines_str = "、".join(sorted(self.banned_lines)) if self.banned_lines else "无"
            pairs_str = "、".join(f"{'∩'.join(p)}" for p in sorted(self.banned_line_pairs)) if self.banned_line_pairs else "无"
            yield event.plain_result(
                f"📌 第五步：玩家B 选择目的地车站\n\n"
                f"🛑 已ban线路：{banned_lines_str}\n"
                f"🛑 已ban线路对：{pairs_str}\n\n"
                f"{p2_name}，请选择你的目的地车站！\n使用：/metro_dest <站名>"
            )

    @filter.command("metro_dest")
    async def cmd_dest(self, event: AstrMessageEvent):
        if self.game_phase != 'ban':
            yield event.plain_result("🚫 当前不在BP阶段")
            return
        if self.setup_step not in (4, 5):
            yield event.plain_result("⏳ 当前不在选目的地阶段")
            return
        user_id = event.get_sender_id()
        expected_idx = 1 if self.setup_step == 4 else 0
        expected = self.duel_players[expected_idx][0]
        if user_id != expected:
            yield event.plain_result("⏳ 当前不是你的回合")
            return
        arg = self._parse_arg(event)
        if not arg:
            yield event.plain_result("请指定车站名称，例如：/metro_dest 大塘")
            return
        station = self.station_aliases.get(arg, arg)
        if station not in self.all_stations:
            yield event.plain_result(f"❌ 车站「{arg}」不是换乘站或不存在")
            return
        if not self._is_station_available_simple(station):
            yield event.plain_result(f"❌ 车站「{station}」已被ban降级，不可选择")
            return
        for pid, dest in self.player_dest.items():
            if dest == station:
                yield event.plain_result(f"❌ 车站「{station}」已被对方选为目的地")
                return
        lines = self._get_station_lines(station)
        if len(lines) < 2:
            yield event.plain_result(f"❌ 车站「{station}」有效线路不足2条")
            return
        # 公平规则：B 选择目的地时线路数必须与 A 的目的地线路数相同
        if expected_idx == 1:
            a_id = self.duel_players[0][0]
            a_dest = self.player_dest.get(a_id)
            if a_dest:
                a_lines = self.player_dest_lines.get(a_id) or self._get_station_lines(a_dest)
                if len(lines) != len(a_lines):
                    yield event.plain_result(
                        f"❌ 公平限制：玩家A的目的地「{a_dest}」为{len(a_lines)}线换乘站，"
                        f"你只能选择{len(a_lines)}线换乘站（「{station}」为{len(lines)}线）"
                    )
                    return
        self.player_dest[user_id] = station
        self.player_dest_lines[user_id] = lines
        player_name = self.duel_players[expected_idx][1]
        lines_str = "、".join(lines)
        yield event.plain_result(f"✅ {player_name} 选择目的地车站「{station}」，线路：{lines_str}")

        self.setup_step += 1
        if self.setup_step == 5:
            yield event.plain_result(f"📌 第六步：玩家A 选择目的地车站\n使用：/metro_dest <站名>")
        elif self.setup_step == 6:
            p1_name = self.duel_players[0][1]
            districts_str = "、".join(sorted(self.districts.keys()))
            yield event.plain_result(
                f"📌 第七步：玩家A 选择出发行政区\n\n"
                f"可用行政区：{districts_str}\n\n"
                f"{p1_name}，请选择出发车站所在的行政区！\n使用：/metro_district <行政区>"
            )

    @filter.command("metro_district")
    async def cmd_district(self, event: AstrMessageEvent):
        if self.game_phase != 'ban':
            yield event.plain_result("🚫 当前不在BP阶段")
            return
        if self.setup_step != 6:
            yield event.plain_result("⏳ 当前不在选行政区阶段")
            return
        user_id = event.get_sender_id()
        expected = self.duel_players[0][0]
        if user_id != expected:
            yield event.plain_result("⏳ 当前不是你的回合")
            return
        arg = self._parse_arg(event)
        if not arg:
            districts_str = "、".join(sorted(self.districts.keys()))
            yield event.plain_result(f"可用行政区：{districts_str}\n使用：/metro_district <行政区>")
            return
        district = arg.strip()
        if district not in self.districts:
            yield event.plain_result(f"❌ 行政区「{district}」不存在")
            return
        self.selected_district = district
        p1_name = self.duel_players[0][1]
        district_stations = self.districts[district]
        available = [s for s in district_stations if s in self.all_stations and self._is_station_available_simple(s)]
        available_str = "、".join(sorted(available)) if available else "无可用车站"
        yield event.plain_result(
            f"✅ {p1_name} 选择出发行政区「{district}」\n\n"
            f"该行政区可用换乘站（共{len(available)}个）：\n{available_str}"
        )
        self.setup_step = 7
        p2_name = self.duel_players[1][1]
        yield event.plain_result(
            f"📌 第八步：玩家B 选择起始车站\n\n"
            f"{p2_name}，请在行政区「{district}」内选择起始车站！\n使用：/metro_startat <站名>"
        )

    @filter.command("metro_startat")
    async def cmd_startat(self, event: AstrMessageEvent):
        if self.game_phase != 'ban':
            yield event.plain_result("🚫 当前不在BP阶段")
            return
        if self.setup_step != 7:
            yield event.plain_result("⏳ 当前不在选起始车站阶段")
            return
        user_id = event.get_sender_id()
        expected = self.duel_players[1][0]
        if user_id != expected:
            yield event.plain_result("⏳ 当前不是你的回合")
            return
        arg = self._parse_arg(event)
        if not arg:
            yield event.plain_result("请指定车站名称，例如：/metro_startat 聚龙")
            return
        station = self.station_aliases.get(arg, arg)
        if station not in self.all_stations:
            yield event.plain_result(f"❌ 车站「{arg}」不是换乘站")
            return
        if not self._is_station_available_simple(station):
            yield event.plain_result(f"❌ 车站「{station}」已被ban降级")
            return
        district_stations = set(self.districts.get(self.selected_district, []))
        if station not in district_stations:
            yield event.plain_result(f"❌ 车站「{station}」不在行政区「{self.selected_district}」内")
            return
        for pid, dest in self.player_dest.items():
            if station == dest:
                yield event.plain_result("❌ 起始车站不能与目的地车站相同")
                return

        # 起始车站不能位于死亡线路（双方3线+目的地共用线路）上
        _l1 = set(self.player_dest_lines.get(self.duel_players[0][0], []))
        _l2 = set(self.player_dest_lines.get(self.duel_players[1][0], []))
        if len(_l1) >= 3 and len(_l2) >= 3 and _l1 & _l2:
            for dl in _l1 & _l2:
                if station in self.current_lines.get(dl, []):
                    yield event.plain_result(
                        f"❌ 起始车站「{station}」位于死亡线路「{dl}」上，不可选择"
                    )
                    return

        self.current_station = station
        self.consumed_stations.add(station)
        p2_name = self.duel_players[1][1]
        yield event.plain_result(f"✅ {p2_name} 选择起始车站「{station}」")

        self.game_phase = 'playing'
        self.current_turn_idx = 0
        self.current_line = ""

        # 死亡线路：双方目的地均为3线及以上且线路存在共用 → 共用线成为死亡线，先进入者判负
        p1_id0 = self.duel_players[0][0]
        p2_id0 = self.duel_players[1][0]
        l1 = set(self.player_dest_lines.get(p1_id0, []))
        l2 = set(self.player_dest_lines.get(p2_id0, []))
        if len(l1) >= 3 and len(l2) >= 3 and (l1 & l2):
            self.dead_lines = l1 & l2

        p1_name = self.duel_players[0][1]
        p1_id = self.duel_players[0][0]
        p2_id = self.duel_players[1][0]
        p1_dest = self.player_dest.get(p1_id, "?")
        p2_dest = self.player_dest.get(p2_id, "?")
        p1_dest_lines = "、".join(self.player_dest_lines.get(p1_id, []))
        p2_dest_lines = "、".join(self.player_dest_lines.get(p2_id, []))
        station_lines_str = "、".join(self._get_station_lines(station))
        moves_str = self._get_move_hints()
        banned_lines_str = "、".join(sorted(self.banned_lines)) if self.banned_lines else "无"
        pairs_str = "、".join(f"{'∩'.join(p)}" for p in sorted(self.banned_line_pairs)) if self.banned_line_pairs else "无"

        dead_lines_str = "、".join(sorted(self.dead_lines)) if self.dead_lines else ""
        dead_tip = f"\n☠️ 死亡线路：{dead_lines_str}（双方目的地共用，谁先进入谁输！）" if self.dead_lines else ""
        yield event.plain_result(
            f"🎮 对战开始！\n\n"
            f"👤 玩家A: {p1_name}（目的地：{p1_dest} [{p1_dest_lines}]）\n"
            f"👤 玩家B: {p2_name}（目的地：{p2_dest} [{p2_dest_lines}]）\n\n"
            f"📍 起始车站：{station}（{station_lines_str}）\n"
            f"🛑 已ban线路：{banned_lines_str}\n"
            f"🛑 已ban线路对：{pairs_str}{dead_tip}\n\n"
            f"📋 规则：必须换乘，到达目的地车站获胜\n"
            f"⚡ 含目的地线路的车站=打击车站\n"
            f"🎴 卡牌不能连续使用\n\n"
            f"📍 当前回合：{p1_name}\n"
            f"🚇 可前往：{moves_str}"
        )

    # ==================== 对战命令 ====================

    @filter.command("metro_use")
    async def cmd_use(self, event: AstrMessageEvent):
        if not self.enabled or self.game_phase != 'playing':
            yield event.plain_result("🚫 游戏未在对战中")
            return
        user_id = event.get_sender_id()
        expected = self.duel_players[self.current_turn_idx][0]
        if user_id != expected:
            yield event.plain_result("⏳ 当前不是你的回合")
            return
        arg = self._parse_arg(event)
        if not arg:
            cards = ", ".join(self.card_lines.keys())
            yield event.plain_result(f"可用卡牌：{cards}\n使用：/metro_use <卡牌> <线路>")
            return
        use_parts = arg.strip().split()
        if len(use_parts) < 2:
            yield event.plain_result("请指定卡牌和目标线路，例如：/metro_use THP1 7号线")
            return
        card_name = use_parts[0].strip().upper()
        target_line = use_parts[1].strip()

        if card_name not in self.card_lines:
            yield event.plain_result(f"❌ 卡牌「{card_name}」不存在")
            return
        if card_name in self.consumed_cards:
            yield event.plain_result(f"❌ 卡牌「{card_name}」已使用过")
            return
        if target_line not in self.current_lines:
            yield event.plain_result(f"❌ 线路「{target_line}」不存在")
            return
        if target_line in self.banned_lines:
            yield event.plain_result(f"❌ 线路「{target_line}」已被ban")
            return
        if self.last_action_was_card:
            yield event.plain_result("❌ 卡牌不能连续使用！请先选站移动")
            return

        card_supported_lines = self.card_lines[card_name]
        if target_line not in card_supported_lines:
            yield event.plain_result(f"❌ 卡牌「{card_name}」不支持转至「{target_line}」\n支持：{', '.join(card_supported_lines)}")
            return

        current_station_lines = self._get_station_lines(self.current_station)
        on_card_line = [l for l in current_station_lines if l in card_supported_lines]
        if not on_card_line:
            yield event.plain_result(f"❌ 当前车站不在卡牌「{card_name}」覆盖的任何线路上")
            return

        p1_id = self.duel_players[0][0]
        p2_id = self.duel_players[1][0]
        all_dest_lines = set(self.player_dest_lines.get(p1_id, []) + self.player_dest_lines.get(p2_id, []))
        if target_line in all_dest_lines:
            yield event.plain_result(f"❌ 不能转至目的地线路「{target_line}」")
            return

        self.consumed_cards.add(card_name)
        self.current_line = target_line
        self.last_action_was_card = True
        player_name = self.duel_players[self.current_turn_idx][1]
        yield event.plain_result(
            f"🎴 {player_name} 使用卡牌「{card_name}」转至「{target_line}」\n"
            f"📍 当前车站：{self.current_station}\n"
            f"🚇 当前线路：{target_line}"
        )

        self.current_turn_idx = 1 - self.current_turn_idx
        next_name = self.duel_players[self.current_turn_idx][1]
        line_stations = self.current_lines.get(target_line, [])
        available_on_line = [s for s in line_stations
                              if s in self.all_stations and s not in self.consumed_stations
                              and s != self.current_station
                              and self._is_station_available_simple(s)]
        available_str = "、".join(sorted(available_on_line)[:10])
        if len(available_on_line) > 10:
            available_str += f" 等{len(available_on_line)}站"
        yield event.plain_result(
            f"📍 现在轮到：{next_name}\n"
            f"💡 必须在「{target_line}」上选站移动\n"
            f"🚇 可选车站：{available_str}"
        )

    @filter.command("metro_stop")
    async def cmd_stop(self, event: AstrMessageEvent):
        if not self.enabled:
            yield event.plain_result("🚫 游戏未在进行中")
            return
        if self.game_phase == 'ban':
            yield event.plain_result("🛑 BP阶段已取消")
            self._reset_game()
            return
        p1_name = self.duel_players[0][1]
        p2_name = self.duel_players[1][1]
        p1_id = self.duel_players[0][0]
        p2_id = self.duel_players[1][0]
        yield event.plain_result(
            f"🛑 对战已停止！\n\n"
            f"📊 对战统计：\n"
            f"已消耗车站：{len(self.consumed_stations)}\n"
            f"已使用卡牌：{len(self.consumed_cards)}\n\n"
            f"👤 {p1_name}（目的地：{self.player_dest.get(p1_id, '?')}）\n"
            f"👤 {p2_name}（目的地：{self.player_dest.get(p2_id, '?')}）"
        )
        self._reset_game()

    @filter.command("metro_status")
    async def cmd_status(self, event: AstrMessageEvent):
        if not self.enabled:
            yield event.plain_result("🚫 游戏未在进行中，使用 /metro_join 加入")
            return
        if self.game_phase == 'ban':
            steps = ["A ban线", "A ban站", "B ban线", "B ban站", "B 选目的地", "A 选目的地", "A 选行政区", "B 选起始"]
            step_desc = steps[self.setup_step] if self.setup_step < len(steps) else "?"
            yield event.plain_result(f"📌 BP阶段进行中（{step_desc}）")
            return
        p1_name = self.duel_players[0][1]
        p2_name = self.duel_players[1][1]
        p1_id = self.duel_players[0][0]
        p2_id = self.duel_players[1][0]
        current_name = self.duel_players[self.current_turn_idx][1]
        station_lines = self._get_station_lines(self.current_station)
        moves = self._get_legal_moves()
        msg = (
            f"🎮 地铁游园对战进行中！\n"
            f"📁 数据集：{self.dataset_name}\n\n"
            f"👤 玩家A: {p1_name}（目的地：{self.player_dest.get(p1_id, '?')}）\n"
            f"👤 玩家B: {p2_name}（目的地：{self.player_dest.get(p2_id, '?')}）\n\n"
            f"📍 当前车站：{self.current_station}（{', '.join(station_lines)}）\n"
            f"🚇 当前线路：{self.current_line or '起始（可任选）'}\n"
            f"可移动：{len(moves)}站\n"
            f"已消耗：{len(self.consumed_stations)} / {len(self.all_stations)}\n"
            f"已用卡牌：{', '.join(sorted(self.consumed_cards)) if self.consumed_cards else '无'}\n"
            f"📍 当前回合：{current_name}"
        )
        if self.strike_line:
            avail = sorted(set(self.current_lines.get(self.strike_station, [])) - {self.strike_line})
            msg += (
                f"\n⚡ 打击中！须避开「{self.strike_line}」，"
                f"仅可走「{self.strike_station}」的其他线路：{'、'.join(avail) if avail else '无'}"
            )
        if self.banned_lines:
            msg += f"\n🛑 禁线：{', '.join(sorted(self.banned_lines))}"
        if self.banned_line_pairs:
            msg += f"\n🛑 禁换乘：{', '.join('∩'.join(p) for p in sorted(self.banned_line_pairs))}"
        yield event.plain_result(msg)

    @filter.command("metro_list")
    async def cmd_list(self, event: AstrMessageEvent):
        if not self.enabled or self.game_phase != 'playing':
            yield event.plain_result("🚫 游戏未在对战中")
            return
        available = sorted(
            s for s in self.all_stations
            if s not in self.consumed_stations and self._is_station_available_simple(s)
        )
        if not available:
            yield event.plain_result("🎉 所有换乘站都已消耗")
            return
        yield event.plain_result(f"🚇 可用换乘站（共{len(available)}个）:\n\n{'、'.join(available)}")

    @filter.command("metro_help")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result("""🌿 地铁游园 - 帮助

📋 游戏阶段：
1. 匹配：/metro_join / /metro_leave / /metro_start
2. BP阶段：ban线ban站 + 选目的地 + 选行政区 + 选出生
3. 对战：轮流移动，必须换乘，打击制胜

📌 BP流程：
  A ban线 → A ban站 → B ban线 → B ban站
  B选目的地 → A选目的地 → A选行政区 → B选起始

  /metro_banline <线路名>       - ban一条线（或skip跳过）
  /metro_bantransfer <线1> <线2> - ban线路对（飞站）
  /metro_dest <站名>             - 选择目的地车站
  /metro_district <行政区>       - 选择出发行政区
  /metro_startat <站名>          - 选择起始车站

🎮 对战命令：
  发送车站名               - 移动到该车站
  发送卡牌+线路            - 使用卡牌（如：THP1转7号线）
  /metro_use <卡牌> <线路>  - 使用卡牌转线
  /metro_stop             - 停止对战
  /metro_status           - 查看状态
  /metro_list             - 查看可用车站
  /metro_help             - 显示帮助

⚡ 规则要点：
  - 每次移动后必须换乘（不能连续同线路）
  - 含目的地线路的车站=打击车站，对方须避开
  - 只有到达目的地车站本身才获胜
  - 有轨/APM卡牌不能连续使用
  - 换乘站和卡牌各只能用一次

🎴 可用卡牌：
  APM: 1/3/5/12/13号线
  THZ1: 3/4/8/11/12号线
  THP1: 6/7/21号线
  THP2: 6/13号线
  TNH1: 广佛/佛山2号线""")

    # ==================== 监听消息（对战阶段） ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        if not self.enabled or self.game_phase != 'playing':
            return
        msg = event.message_str.strip()
        if not msg:
            return
        if msg.startswith("/"):
            return

        sender_id = event.get_sender_id()
        if sender_id != self.duel_players[self.current_turn_idx][0]:
            return

        # ---- 卡牌识别（如 "THP1转7" 或 "APM 1号线"）----
        card_match = re.match(
            r'(APM|THZ1|THP1|THP2|TNH1)\s*[转 ]\s*(.+)',
            msg, re.IGNORECASE
        )
        if card_match:
            card_name = card_match.group(1).upper()
            target_raw = card_match.group(2).strip()
            target_line = target_raw
            for line_name in self.current_lines:
                if target_raw in line_name or line_name in target_raw:
                    target_line = line_name
                    break
            event.message_str = f"/metro_use {card_name} {target_line}"
            async for result in self.cmd_use(event):
                yield result
            return

        # ---- 查找车站名 ----
        matches = list(self.station_pattern.finditer(msg))
        if not matches:
            return

        current_player_id = self.duel_players[self.current_turn_idx][0]
        player_name = self.duel_players[self.current_turn_idx][1]
        player_dest = self.player_dest.get(current_player_id, "")

        other_idx = 1 - self.current_turn_idx
        other_id = self.duel_players[other_idx][0]
        other_dest = self.player_dest.get(other_id, "")

        for match in matches:
            station = self.station_aliases.get(match.group(1), match.group(1))
            if station not in self.all_stations:
                continue
            if station == self.current_station:
                continue
            if not self._is_station_available_simple(station):
                continue

            # 打击时：只能前往打击车站其他线路（非打击线）上的换乘站
            if self.strike_line:
                strike_avail = set(self.current_lines.get(self.strike_station, [])) - {self.strike_line}
                station_lines = set(
                    l for l, sts in self.current_lines.items() if station in sts
                )
                if not (station_lines & strike_avail):
                    continue
            else:
                # 正常状态：必须换乘（不可停留在当前线路）
                shared = self._stations_share_line(self.current_station, station, self.current_line)
                if not shared and self.current_line:
                    continue

            # 死亡线路车站不可前往（进入即判负）
            if self.dead_lines and any(
                station in self.current_lines.get(dl, []) for dl in self.dead_lines
            ):
                continue

            # 消耗检查
            if station in self.consumed_stations:
                if station != player_dest and station != other_dest:
                    continue

            # ---- 有效移动 ----
            used_line = shared[0] if shared else self._get_station_lines(station)[0]
            old_station = self.current_station
            self.current_station = station
            self.current_line = used_line
            self.consumed_stations.add(station)
            self.last_action_was_card = False

            feedback = f"✅ {player_name} 从{old_station}移动到了{station}（通过{used_line}）"

            # ---- 获胜检查：只有到达目的地车站本身才获胜 ----
            if station == player_dest:
                feedback += f"\n\n🎉 {player_name} 成功抵达目的地车站「{station}」！"
                yield event.plain_result(feedback)
                yield event.plain_result(self._announce_winner(self.current_turn_idx, f"抵达目的地车站「{station}」"))
                self._reset_game()
                return

            # ---- 误入对方目的地车站 ----
            if station == other_dest:
                feedback += f"\n\n💥 {player_name} 误入对方目的地车站「{station}」！对方获胜！"
                yield event.plain_result(feedback)
                yield event.plain_result(self._announce_winner(other_idx, f"对方误入目的地车站「{station}」"))
                self._reset_game()
                return

            # ---- 死亡线路检查：双方目的地均3线+且共用线路时，谁先进入共用线谁输 ----
            if self.dead_lines:
                for dl in self.dead_lines:
                    if station in self.current_lines.get(dl, []):
                        other_name_d = self.duel_players[other_idx][1]
                        feedback += f"\n\n☠️ {player_name} 率先进入死亡线路「{dl}」的车站「{station}」！{other_name_d} 获胜！"
                        yield event.plain_result(feedback)
                        yield event.plain_result(self._announce_winner(other_idx, f"对方率先进入死亡线路「{dl}」"))
                        self._reset_game()
                        return

            # ---- 清除上一轮打击（对方已避开或本回合重新判定）----
            self.strike_line = ""
            self.strike_attacker = ""
            self.strike_station = ""

            # ---- 检查打击车站（含己方目的地线路但不是目的地车站本身）----
            hit_lines = self._check_strike(station, current_player_id)
            if hit_lines:
                self.strike_line = hit_lines[0]
                self.strike_attacker = current_player_id
                self.strike_station = station
                lines_str = "、".join(hit_lines)
                other_name = self.duel_players[other_idx][1]
                avail_lines = sorted(
                    set(self.current_lines.get(station, [])) - set(hit_lines)
                )
                avail_str = "、".join(avail_lines) if avail_lines else "无"
                feedback += (
                    f"\n\n⚡ 打击！{player_name} 锁定线路「{lines_str}」！\n"
                    f"💡 {other_name} 不得进入「{lines_str}」的车站，"
                    f"仅可前往「{station}」的其他线路（{avail_str}）上的换乘站避开打击"
                )

            # ---- 切换回合 ----
            self.current_turn_idx = 1 - self.current_turn_idx
            next_name = self.duel_players[self.current_turn_idx][1]
            next_moves = self._get_legal_moves()

            if not next_moves:
                feedback += f"\n\n⛔ {next_name} 无合法移动！{player_name} 获胜！"
                yield event.plain_result(feedback)
                yield event.plain_result(self._announce_winner(1 - self.current_turn_idx, "对方无合法移动"))
                self._reset_game()
                return

            moves_str = self._get_move_hints()
            feedback += f"\n\n📍 现在轮到：{next_name}（在{station}）"
            feedback += f"\n💡 可前往：{moves_str}"
            feedback += f"\n已消耗：{len(self.consumed_stations)} / {len(self.all_stations)}"

            yield event.plain_result(feedback)
            logger.info(f"玩家 {sender_id} 从 {old_station} 移动到 {station} (线路: {used_line})")
            return

        yield event.plain_result("❌ 未找到合法的换乘站，请检查车站名或线路连接")
