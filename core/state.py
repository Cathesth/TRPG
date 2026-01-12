"""
게임 상태 관리 싱글톤
"""
from typing import Dict, Any, Optional, List, Union
from config import DEFAULT_CONFIG
import copy
import re
import logging

logger = logging.getLogger(__name__)


class GameState:
    """
    게임 상태를 관리하는 싱글톤 클래스
    여러 모듈에서 공유되는 상태를 중앙에서 관리
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """초기 상태 설정"""
        self._config = DEFAULT_CONFIG.copy()
        self._state: Optional[Dict[str, Any]] = None
        self._game_graph = None

    @property
    def config(self) -> Dict[str, Any]:
        return self._config

    @config.setter
    def config(self, value: Dict[str, Any]):
        self._config = value

    @property
    def state(self) -> Optional[Dict[str, Any]]:
        return self._state

    @state.setter
    def state(self, value: Optional[Dict[str, Any]]):
        self._state = value

    @property
    def game_graph(self):
        return self._game_graph

    @game_graph.setter
    def game_graph(self, value):
        self._game_graph = value

    def clear(self):
        """상태 초기화"""
        self._state = None
        self._game_graph = None


class WorldState:
    """
    🛠️ World State Manager (규칙 기반 상태 관리)

    LLM 환각(Hallucination)을 방지하기 위한 규칙 기반 상태 관리자.
    LLM이 직접 수정할 수 없으며, 사전에 정의된 로직으로만 상태 변경.

    관리 항목:
    - World: 시간, 위치, 전역 플래그, 턴 카운트
    - NPC States: 생존 여부, HP, 감정, 관계도, 위치, 개별 플래그
    - Player Stats: HP, 골드, 정신력, 방사능, 인벤토리, 퀘스트, 플래그
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """초기 상태 설정"""
        # A. World (전역 상태)
        self.time = {"day": 1, "phase": "morning"}  # morning|afternoon|night
        self.location = None  # current_scene_id
        self.global_flags: Dict[str, bool] = {}  # 전역 이벤트 플래그
        self.turn_count = 0  # 전체 게임 진행 턴 수

        # B. NPC States (가변 영역) - HP와 위치 추가
        self.npcs: Dict[str, Dict[str, Any]] = {}
        # 구조: { "npc_id": {
        #   "status": "alive|dead|wounded",
        #   "hp": 100,
        #   "max_hp": 100,
        #   "emotion": "neutral",
        #   "relationship": 50,
        #   "is_hostile": False,
        #   "location": "scene_id",
        #   "flags": {}
        # } }

        # C. Player Stats
        self.player = {
            "hp": 100,
            "max_hp": 100,
            "gold": 0,
            "sanity": 100,
            "radiation": 0,
            "inventory": [],
            "quests": {},  # { "quest_id": "active|completed|failed" }
            "flags": {},  # 플레이어 고유 이벤트 플래그
            "custom_stats": {}  # 시나리오별 커스텀 스탯
        }

        # 상태 변경 히스토리 (디버깅/분석용)
        self.history: List[Dict[str, Any]] = []

    def reset(self):
        """상태 완전 초기화"""
        self._initialize()
        logger.info("WorldState has been reset")

    # ========================================
    # 1. 초기화 및 로딩
    # ========================================

    def initialize_from_scenario(self, scenario_data: Dict[str, Any]):
        """
        시나리오 데이터로부터 초기 상태를 설정

        Args:
            scenario_data: 시나리오 JSON 데이터
        """
        # 플레이어 초기 스탯 설정
        initial_state = scenario_data.get("initial_state", {})

        if "hp" in initial_state:
            self.player["hp"] = initial_state["hp"]
            self.player["max_hp"] = initial_state.get("max_hp", initial_state["hp"])

        if "inventory" in initial_state:
            self.player["inventory"] = list(initial_state["inventory"])

        # 커스텀 스탯 로드 (sanity, radiation 등)
        for key, value in initial_state.items():
            if key not in ["hp", "max_hp", "inventory"]:
                self.player["custom_stats"][key] = value

        # 시작 위치 설정
        self.location = scenario_data.get("start_scene_id")

        # NPC 초기 상태 설정
        npcs_data = scenario_data.get("npcs", [])
        for npc in npcs_data:
            if isinstance(npc, dict) and "name" in npc:
                name = npc["name"]
                self.npcs[name] = {
                    "status": "alive",
                    "emotion": "neutral",
                    "relationship": 50,  # 중립
                    "flags": {}
                }
                # 적인 경우 초기 관계도를 낮게 설정
                if npc.get("isEnemy"):
                    self.npcs[name]["relationship"] = 0
                    self.npcs[name]["emotion"] = "hostile"

        logger.info(f"WorldState initialized from scenario: {scenario_data.get('title', 'Unknown')}")

    # ========================================
    # 2. 상태 업데이트 (핵심 로직)
    # ========================================

    def update_state(self, effect_data: Union[Dict[str, Any], List[Dict[str, Any]]]):
        """
        효과 데이터를 받아 상태를 업데이트 (순수 규칙 기반, LLM 개입 없음)

        Args:
            effect_data: 효과 데이터 (단일 dict 또는 list)
                예시: {"hp": -10, "gold": +5, "item_add": "포션"}
                      [{"hp": -10}, {"npc": "노인 J", "relationship": +10}]

        지원 효과:
        - hp, gold, sanity, radiation 등: 수치 증감
        - item_add, item_remove: 아이템 추가/제거
        - npc: NPC 이름과 함께 relationship, emotion, status, flags 변경
        - global_flag: 전역 플래그 설정
        - quest_start, quest_complete, quest_fail: 퀘스트 상태 변경
        """
        if not effect_data:
            return

        # 리스트가 아니면 리스트로 변환
        if isinstance(effect_data, dict):
            effect_data = [effect_data]

        for effect in effect_data:
            if not isinstance(effect, dict):
                continue

            # 히스토리 기록
            self.history.append({
                "effect": copy.deepcopy(effect),
                "before": self._get_snapshot()
            })

            # 플레이어 스탯 변경
            for stat in ["hp", "gold", "sanity", "radiation"]:
                if stat in effect:
                    self._update_player_stat(stat, effect[stat])

            # 커스텀 스탯 변경
            for key, value in effect.items():
                if key in self.player["custom_stats"]:
                    self._update_player_stat(key, value, is_custom=True)

            # 아이템 관리
            if "item_add" in effect:
                self._add_item(effect["item_add"])
            if "item_remove" in effect:
                self._remove_item(effect["item_remove"])

            # NPC 관계 변경
            if "npc" in effect:
                npc_name = effect["npc"]
                self._update_npc_state(npc_name, effect)

            # 전역 플래그
            if "global_flag" in effect:
                flag_name = effect["global_flag"]
                flag_value = effect.get("value", True)
                self.global_flags[flag_name] = flag_value

            # 퀘스트 관리
            if "quest_start" in effect:
                self.player["quests"][effect["quest_start"]] = "active"
            if "quest_complete" in effect:
                self.player["quests"][effect["quest_complete"]] = "completed"
            if "quest_fail" in effect:
                self.player["quests"][effect["quest_fail"]] = "failed"

    def _update_player_stat(self, stat_name: str, value: Union[int, float], is_custom: bool = False):
        """플레이어 스탯 업데이트 (증감 계산)"""
        target = self.player["custom_stats"] if is_custom else self.player

        if stat_name not in target:
            target[stat_name] = 0

        # 상대값 계산 (문자열로 "+10", "-5" 등)
        if isinstance(value, str):
            value = value.strip()
            if value.startswith('+') or value.startswith('-'):
                try:
                    delta = int(value)
                    target[stat_name] += delta
                except ValueError:
                    pass
            else:
                try:
                    target[stat_name] = int(value)
                except ValueError:
                    pass
        elif isinstance(value, (int, float)):
            # 숫자가 양수/음수에 따라 증감
            target[stat_name] += value

        # HP는 max_hp를 넘지 않도록
        if stat_name == "hp":
            target["hp"] = max(0, min(target["hp"], target.get("max_hp", 999)))

        # 음수 방지 (일부 스탯)
        if stat_name in ["gold", "radiation", "sanity"]:
            target[stat_name] = max(0, target[stat_name])

    def _add_item(self, item: Union[str, List[str]]):
        """아이템 추가"""
        if isinstance(item, str):
            if item not in self.player["inventory"]:
                self.player["inventory"].append(item)
        elif isinstance(item, list):
            for i in item:
                if i not in self.player["inventory"]:
                    self.player["inventory"].append(i)

    def _remove_item(self, item: Union[str, List[str]]):
        """아이템 제거"""
        if isinstance(item, str):
            if item in self.player["inventory"]:
                self.player["inventory"].remove(item)
        elif isinstance(item, list):
            for i in item:
                if i in self.player["inventory"]:
                    self.player["inventory"].remove(i)

    def _update_npc_state(self, npc_name: str, effect: Dict[str, Any]):
        """NPC 상태 업데이트"""
        if npc_name not in self.npcs:
            # NPC가 없으면 초기화
            self.npcs[npc_name] = {
                "status": "alive",
                "emotion": "neutral",
                "relationship": 50,
                "flags": {}
            }

        npc = self.npcs[npc_name]

        # 관계도 변경
        if "relationship" in effect:
            delta = effect["relationship"]
            if isinstance(delta, (int, float)):
                npc["relationship"] += delta
                npc["relationship"] = max(0, min(100, npc["relationship"]))

        # 감정 변경
        if "emotion" in effect:
            npc["emotion"] = effect["emotion"]

        # 생존 여부
        if "status" in effect:
            npc["status"] = effect["status"]

        # NPC 개별 플래그
        if "npc_flag" in effect:
            flag_name = effect["npc_flag"]
            flag_value = effect.get("flag_value", True)
            npc["flags"][flag_name] = flag_value

        # HP 변경 (적용 예: {"npc": "노인 J", "hp": -10})
        if "hp" in effect:
            hp_change = effect["hp"]
            if isinstance(hp_change, (int, float)):
                npc["hp"] = npc.get("hp", 100) + hp_change
                npc["hp"] = max(0, min(npc["hp"], npc.get("max_hp", 100)))

        # 위치 변경 (적용 예: {"npc": "노인 J", "location": "다리 위"})
        if "location" in effect:
            npc["location"] = effect["location"]

    # ========================================
    # 3. 조건 체크 (Condition Checker)
    # ========================================

    def check_condition(self, condition: Union[str, Dict[str, Any]]) -> bool:
        """
        조건 문자열 또는 딕셔너리를 평가하여 불리언 반환

        Args:
            condition: 조건 문자열 (예: "hp > 50", "gold >= 100", "has_item:포션")
                      또는 딕셔너리 (예: {"stat": "hp", "op": ">", "value": 50})

        Returns:
            조건 충족 여부 (True/False)
        """
        if not condition:
            return True

        if isinstance(condition, dict):
            return self._check_condition_dict(condition)
        elif isinstance(condition, str):
            return self._check_condition_string(condition)

        return False

    def _check_condition_dict(self, condition: Dict[str, Any]) -> bool:
        """딕셔너리 형태의 조건 체크"""
        cond_type = condition.get("type", "stat")

        if cond_type == "stat":
            stat_name = condition.get("stat")
            operator = condition.get("op", ">=")
            value = condition.get("value", 0)

            current_value = self.get_stat(stat_name)
            if current_value is None:
                return False

            return self._compare(current_value, operator, value)

        elif cond_type == "item":
            item_name = condition.get("item")
            return item_name in self.player["inventory"]

        elif cond_type == "flag":
            flag_name = condition.get("flag")
            return self.global_flags.get(flag_name, False)

        elif cond_type == "npc":
            npc_name = condition.get("npc")
            npc_field = condition.get("field", "status")
            operator = condition.get("op", "==")
            value = condition.get("value")

            if npc_name not in self.npcs:
                return False

            current_value = self.npcs[npc_name].get(npc_field)
            return self._compare(current_value, operator, value)

        return False

    def _check_condition_string(self, condition: str) -> bool:
        """문자열 형태의 조건 체크"""
        condition = condition.strip()

        # has_item:아이템명
        if condition.startswith("has_item:"):
            item_name = condition.split(":", 1)[1].strip()
            return item_name in self.player["inventory"]

        # flag:플래그명
        if condition.startswith("flag:"):
            flag_name = condition.split(":", 1)[1].strip()
            return self.global_flags.get(flag_name, False)

        # 스탯 비교 (예: "hp > 50", "gold >= 100")
        match = re.match(r'(\w+)\s*(>=|<=|==|!=|>|<)\s*(\d+)', condition)
        if match:
            stat_name = match.group(1)
            operator = match.group(2)
            value = int(match.group(3))

            current_value = self.get_stat(stat_name)
            if current_value is None:
                return False

            return self._compare(current_value, operator, value)

        return False

    def _compare(self, a: Any, op: str, b: Any) -> bool:
        """비교 연산자 평가"""
        try:
            if op == ">=": return a >= b
            elif op == "<=": return a <= b
            elif op == ">": return a > b
            elif op == "<": return a < b
            elif op == "==": return a == b
            elif op == "!=": return a != b
        except:
            return False
        return False

    # ========================================
    # 4. 상태 조회 (Getter)
    # ========================================

    def get_stat(self, stat_name: str) -> Optional[Union[int, float]]:
        """플레이어 스탯 조회"""
        if stat_name in self.player:
            return self.player[stat_name]
        if stat_name in self.player["custom_stats"]:
            return self.player["custom_stats"][stat_name]
        return None

    def get_npc_state(self, npc_name: str) -> Optional[Dict[str, Any]]:
        """NPC 상태 조회"""
        return self.npcs.get(npc_name)

    def has_item(self, item_name: str) -> bool:
        """아이템 소지 여부 확인"""
        return item_name in self.player["inventory"]

    def get_inventory(self) -> List[str]:
        """인벤토리 목록 반환"""
        return list(self.player["inventory"])

    # ========================================
    # 5. LLM 프롬프트용 컨텍스트 생성
    # ========================================

    def get_context_for_llm(self) -> str:
        """
        LLM 프롬프트에 주입할 현재 상태를 텍스트로 변환

        Returns:
            현재 상태를 요약한 텍스트
        """
        lines = ["=== 현재 게임 상태 ===\n"]

        # 플레이어 상태
        lines.append("[플레이어]")
        lines.append(f"- HP: {self.player['hp']}/{self.player['max_hp']}")
        lines.append(f"- 골드: {self.player.get('gold', 0)}")

        for key, value in self.player["custom_stats"].items():
            lines.append(f"- {key}: {value}")

        if self.player["inventory"]:
            lines.append(f"- 소지품: {', '.join(self.player['inventory'])}")
        else:
            lines.append("- 소지품: 없음")

        # 위치
        if self.location:
            lines.append(f"\n[현재 위치] {self.location}")

        # 시간
        lines.append(f"\n[시간] {self.time['day']}일차 - {self.time['phase']}")

        # NPC 관계도 (중요한 것만)
        if self.npcs:
            lines.append("\n[NPC 상태]")
            for npc_name, npc_data in self.npcs.items():
                if npc_data["status"] != "alive":
                    lines.append(f"- {npc_name}: {npc_data['status']}")
                elif npc_data["relationship"] != 50:
                    lines.append(f"- {npc_name}: 관계도 {npc_data['relationship']}, {npc_data['emotion']}")

        # 전역 플래그 (활성화된 것만)
        active_flags = [k for k, v in self.global_flags.items() if v]
        if active_flags:
            lines.append(f"\n[활성 플래그] {', '.join(active_flags)}")

        return "\n".join(lines)

    # ========================================
    # 6. 시간 진행
    # ========================================

    def advance_time(self, steps: int = 1):
        """
        서사적 시간을 진행

        Args:
            steps: 진행할 단계 수 (1 = 한 단계)
        """
        phases = ["morning", "afternoon", "night"]

        for _ in range(steps):
            current_idx = phases.index(self.time["phase"])
            next_idx = (current_idx + 1) % len(phases)

            self.time["phase"] = phases[next_idx]

            # 하루가 지남
            if next_idx == 0:
                self.time["day"] += 1

        logger.info(f"Time advanced to Day {self.time['day']}, {self.time['phase']}")

    # ========================================
    # 7. 데이터 영속성 (Persistence)
    # ========================================

    def to_dict(self) -> Dict[str, Any]:
        """현재 상태를 딕셔너리로 직렬화 (저장용)"""
        return {
            "time": copy.deepcopy(self.time),
            "location": self.location,
            "global_flags": copy.deepcopy(self.global_flags),
            "npcs": copy.deepcopy(self.npcs),
            "player": copy.deepcopy(self.player),
            "history": copy.deepcopy(self.history[-50:])  # 최근 50개만
        }

    def from_dict(self, data: Dict[str, Any]):
        """딕셔너리로부터 상태 복원 (로드용)"""
        if not data:
            return

        self.time = data.get("time", {"day": 1, "phase": "morning"})
        self.location = data.get("location")
        self.global_flags = data.get("global_flags", {})
        self.npcs = data.get("npcs", {})
        self.player = data.get("player", {})
        self.history = data.get("history", [])

        logger.info("WorldState restored from saved data")

    def _get_snapshot(self) -> Dict[str, Any]:
        """현재 상태 스냅샷 (히스토리용)"""
        return {
            "player_hp": self.player.get("hp"),
            "player_gold": self.player.get("gold"),
            "location": self.location
        }

    # ========================================
    # 8. NPC HP 관리 및 불사신 방지 (핵심 로직)
    # ========================================

    def update_npc_hp(self, npc_id: str, amount: int) -> Dict[str, Any]:
        """
        NPC 체력을 증감시키고, HP가 0 이하가 되면 즉시 status를 "dead"로 변경

        ⚠️ 불사신 방지 핵심 로직: LLM이 아닌 Python 산술 연산으로만 처리

        Args:
            npc_id: NPC 식별자 (이름 또는 ID)
            amount: 증감량 (음수면 데미지, 양수면 회복)

        Returns:
            결과 정보 {"npc_id": str, "hp": int, "status": str, "is_dead": bool}
        """
        # NPC가 없으면 초기화
        if npc_id not in self.npcs:
            logger.warning(f"NPC '{npc_id}' not found. Initializing with default values.")
            self.npcs[npc_id] = {
                "status": "alive",
                "hp": 100,
                "max_hp": 100,
                "emotion": "neutral",
                "relationship": 50,
                "is_hostile": False,
                "location": self.location,
                "flags": {}
            }

        npc = self.npcs[npc_id]

        # 이미 죽은 NPC는 HP 변경 불가
        if npc.get("status") == "dead":
            logger.info(f"NPC '{npc_id}' is already dead. HP change ignored.")
            return {
                "npc_id": npc_id,
                "hp": 0,
                "max_hp": npc.get("max_hp", 100),
                "status": "dead",
                "is_dead": True
            }

        # HP 변경 적용 (Python 산술 연산)
        old_hp = npc.get("hp", 100)
        npc["hp"] = old_hp + amount
        max_hp = npc.get("max_hp", 100)

        # HP 범위 제한 (0 ~ max_hp)
        npc["hp"] = max(0, min(npc["hp"], max_hp))

        # 🛠️ 불사신 방지: HP가 0 이하면 강제로 status를 "dead"로 변경
        if npc["hp"] <= 0:
            npc["status"] = "dead"
            logger.info(f"⚰️ NPC '{npc_id}' has died. HP: {old_hp} -> 0")
        elif npc["hp"] < max_hp * 0.3:
            # HP가 30% 이하면 "wounded" 상태로 변경
            if npc.get("status") != "wounded":
                npc["status"] = "wounded"
                logger.info(f"🩹 NPC '{npc_id}' is wounded. HP: {npc['hp']}/{max_hp}")

        # 히스토리 기록
        self.history.append({
            "action": "update_npc_hp",
            "npc_id": npc_id,
            "hp_change": amount,
            "hp_before": old_hp,
            "hp_after": npc["hp"],
            "status": npc["status"]
        })

        return {
            "npc_id": npc_id,
            "hp": npc["hp"],
            "max_hp": max_hp,
            "status": npc["status"],
            "is_dead": npc["status"] == "dead"
        }

    def is_npc_alive(self, npc_id: str) -> bool:
        """
        NPC 생존 여부 확인 (전투 로그 생성 전 필수 검증)

        Args:
            npc_id: NPC 식별자

        Returns:
            생존 여부 (True: 살아있음, False: 죽었거나 없음)
        """
        if npc_id not in self.npcs:
            return False

        return self.npcs[npc_id].get("status") == "alive"

    def get_alive_npcs_in_location(self, location: Optional[str] = None) -> List[str]:
        """
        특정 위치에 살아있는 NPC 목록 반환

        Args:
            location: 위치 ID (None이면 현재 플레이어 위치)

        Returns:
            살아있는 NPC ID 리스트
        """
        if location is None:
            location = self.location

        alive_npcs = []
        for npc_id, npc_data in self.npcs.items():
            if npc_data.get("status") == "alive" and npc_data.get("location") == location:
                alive_npcs.append(npc_id)

        return alive_npcs

    # ========================================
    # 9. 인벤토리 검증 (LLM 환각 방지)
    # ========================================

    def validate_inventory_action(self, item_id: str, action: str = "use") -> Dict[str, Any]:
        """
        아이템 사용/제거 전 실제 인벤토리에 있는지 검증

        Args:
            item_id: 아이템 식별자
            action: "use" | "remove" | "equip"

        Returns:
            {"valid": bool, "message": str, "item_id": str}
        """
        if item_id not in self.player["inventory"]:
            logger.warning(f"❌ Inventory validation failed: '{item_id}' not in inventory")
            return {
                "valid": False,
                "message": f"'{item_id}'을(를) 소지하고 있지 않습니다.",
                "item_id": item_id
            }

        logger.info(f"✅ Inventory validation passed: '{item_id}' is available")
        return {
            "valid": True,
            "message": f"'{item_id}'을(를) {action}합니다.",
            "item_id": item_id
        }

    def use_item(self, item_id: str, consume: bool = True) -> bool:
        """
        아이템 사용 (검증 포함)

        Args:
            item_id: 아이템 식별자
            consume: 사용 후 소모 여부

        Returns:
            사용 성공 여부
        """
        validation = self.validate_inventory_action(item_id, "use")

        if not validation["valid"]:
            return False

        # 소모성 아이템이면 제거
        if consume:
            self._remove_item(item_id)
            logger.info(f"🔥 Item consumed: '{item_id}'")

        return True

    # ========================================
    # 10. 턴 관리
    # ========================================

    def increment_turn(self):
        """
        게임 턴 카운트 증가 (매 행동마다 호출)
        """
        self.turn_count += 1
        logger.debug(f"Turn count: {self.turn_count}")

        # 턴마다 방사능 피해 적용 (예시)
        if self.player.get("radiation", 0) >= 100:
            radiation_damage = -5
            self.update_state({"hp": radiation_damage})
            logger.warning(f"☢️ Radiation damage: {radiation_damage} HP")

    def get_turn_count(self) -> int:
        """현재 턴 수 반환"""
        return self.turn_count

    # ========================================
    # 11. NPC 초기화 헬퍼
    # ========================================

    def register_npc(
        self,
        npc_id: str,
        hp: int = 100,
        max_hp: int = 100,
        is_hostile: bool = False,
        location: Optional[str] = None,
        **kwargs
    ):
        """
        새로운 NPC를 등록 (시나리오 빌더용)

        Args:
            npc_id: NPC 식별자
            hp: 초기 체력
            max_hp: 최대 체력
            is_hostile: 적대 여부
            location: 위치
            **kwargs: 추가 속성 (emotion, relationship 등)
        """
        if npc_id in self.npcs:
            logger.warning(f"NPC '{npc_id}' already exists. Overwriting.")

        self.npcs[npc_id] = {
            "status": "alive",
            "hp": hp,
            "max_hp": max_hp,
            "is_hostile": is_hostile,
            "location": location or self.location,
            "emotion": kwargs.get("emotion", "neutral"),
            "relationship": kwargs.get("relationship", 50 if not is_hostile else 0),
            "flags": {}
        }

        logger.info(f"✨ NPC registered: '{npc_id}' (HP: {hp}/{max_hp}, Hostile: {is_hostile})")

    # ========================================
    # 12. 디버깅 및 상태 덤프
    # ========================================

    def dump_state(self) -> str:
        """
        전체 상태를 상세하게 텍스트로 출력 (디버깅용)

        Returns:
            전체 상태를 포함한 텍스트
        """
        lines = ["=" * 60]
        lines.append("🎮 WORLD STATE DUMP")
        lines.append("=" * 60)
        lines.append(f"\n[World Info]")
        lines.append(f"  Turn: {self.turn_count}")
        lines.append(f"  Location: {self.location}")
        lines.append(f"  Time: Day {self.time['day']}, {self.time['phase']}")

        lines.append(f"\n[Player Stats]")
        for key, value in self.player.items():
            if key not in ["custom_stats", "quests", "flags"]:
                lines.append(f"  {key}: {value}")

        if self.player.get("custom_stats"):
            lines.append(f"\n[Custom Stats]")
            for key, value in self.player["custom_stats"].items():
                lines.append(f"  {key}: {value}")

        lines.append(f"\n[NPCs] (Total: {len(self.npcs)})")
        for npc_id, npc_data in self.npcs.items():
            status_emoji = "💀" if npc_data["status"] == "dead" else "🩹" if npc_data["status"] == "wounded" else "✅"
            lines.append(f"  {status_emoji} {npc_id}:")
            lines.append(f"     Status: {npc_data.get('status', 'unknown')}")
            lines.append(f"     HP: {npc_data.get('hp', '?')}/{npc_data.get('max_hp', '?')}")
            lines.append(f"     Location: {npc_data.get('location', 'unknown')}")
            lines.append(f"     Hostile: {npc_data.get('is_hostile', False)}")
            lines.append(f"     Relationship: {npc_data.get('relationship', 50)}")

        if self.global_flags:
            lines.append(f"\n[Global Flags]")
            for flag, value in self.global_flags.items():
                lines.append(f"  {flag}: {value}")

        lines.append("=" * 60)

        return "\n".join(lines)
