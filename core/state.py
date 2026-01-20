"""
게임 상태 관리 클래스
"""
from typing import Dict, Any, Optional, List, Union
from config import DEFAULT_CONFIG
import copy
import re
import logging
import difflib

logger = logging.getLogger(__name__)


class GameState:
    """
    게임 상태를 관리하는 클래스 (세션별 독립 인스턴스)
    여러 모듈에서 공유되는 상태를 관리하되, 세션마다 별도 인스턴스 사용
    """

    def __init__(self):
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

    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환 (직렬화)"""
        return {
            "config": self._config,
            "state": self._state,
            # game_graph는 직렬화하지 않음 (런타임에 재생성)
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GameState':
        """딕셔너리에서 복원 (역직렬화)"""
        instance = cls()
        instance._config = data.get("config", DEFAULT_CONFIG.copy())
        instance._state = data.get("state")
        # game_graph는 별도로 재생성 필요
        return instance


class WorldState:
    """
    핵심 World State Manager (규칙 기반 상태 관리)

    LLM 환각(Hallucination)을 방지하기 위한 규칙 기반 상태 관리자.
    LLM이 직접 수정할 수 없으며 사전에 정의된 로직으로만 상태 변경

    관리 대상:
    - World: 시간, 위치, 지역 플래그 등 카운터
    - NPC States: 생존 여부, HP, 감정, 관계도, 위치, 개별 플래그
    - Player Stats: HP, 골드, 정신력, 방사능, 인벤토리, 스킬 플래그
    - Narrative History: LLM 단기 기억을 위한 서사적 이벤트 기록 (슬라이딩 윈도우)
    """

    # ✅ 싱글톤 패턴 완전 제거 - 세션별로 독립적인 인스턴스 사용
    def __init__(self):
        """초기 상태 설정"""
        # A. World (지역 상태)
        self.time = {"day": 1, "phase": "morning"}  # morning/afternoon/night
        self.location = None  # current_scene_id
        self.global_flags: Dict[str, bool] = {}  # 지역 이벤트 플래그
        self.turn_count = 1  # 전체 게임 진행 턴수

        # B. NPC States (개별 영역) - HP와 위치 추가
        self.npcs: Dict[str, Dict[str, Any]] = {}
        # 구조: { "npc_id": {
        #   "status": "alive/dead/wounded",
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
            "quests": {},  # { "quest_id": "active/completed/failed" }
            "flags": {},  # 플레이어 고유 이벤트 플래그
            "custom_stats": {}  # 시나리오별 커스텀 스탯
        }

        # 상태 변경 히스토리 (디버깅용)
        self.history: List[Dict[str, Any]] = []

        # D. Narrative History (서사 기억 시스템)
        self.narrative_history: List[str] = []
        self.max_narrative_history = 10  # 슬라이딩 윈도우 크기

        # E. Item Registry (아이템 레지스트리 - 객체 지향 아이템 관리)
        # 시나리오의 items 정의를 기반으로 {"아이템명": Item객체} 딕셔너리 구성
        self.item_registry: Dict[str, Any] = {}

    def reset(self):
        """상태 완전 초기화"""
        self.__init__()
        logger.info("WorldState has been reset")

    def add_narrative_event(self, text: str):
        """
        서사적 이벤트를 기록 (LLM 단기 기억 강화용)
        중복 방지: 직전 기록과 동일한 내용은 추가하지 않음

        Args:
            text: 기록할 서사적 이벤트 문장
        """
        if not text or not text.strip():
            return

        text = text.strip()

        # 🔴 Turn 번호가 이미 포함된 경우 제거 (중복 방지)
        if text.startswith("[Turn "):
            # 이미 턴 번호가 있으면 제거
            import re
            text = re.sub(r'^\[Turn \d+]\s*', '', text)

        # 🔴 중복 방지: 직전 기록과 동일하면 무시
        prefixed_text = f"[Turn {self.turn_count}] {text}"

        if self.narrative_history and self.narrative_history[-1] == prefixed_text:
            logger.debug(f"[NARRATIVE] Duplicate event ignored: {prefixed_text}")
            return

        # ✅ 작업 4: 턴 번호 접두사 추가 (시간 순서 명확화)
        self.narrative_history.append(prefixed_text)

        # 슬라이딩 윈도우: 10개를 넘으면 가장 오래된 것부터 제거
        if len(self.narrative_history) > self.max_narrative_history:
            # 간단한 전략: 가장 오래된 것 제거
            self.narrative_history.pop(0)
            logger.debug(f"[NARRATIVE] History trimmed, size: {len(self.narrative_history)}")

        logger.info(f"📖 [NARRATIVE] Event added: {prefixed_text}")

    # ========================================
    # 1. 초기화 및 로딩
    # ========================================

    def initialize_from_scenario(self, scenario_data: Dict[str, Any]):
        """
        시나리오 데이터로부터 초기 상태를 설정

        Args:
            scenario_data: 시나리오 JSON 데이터
        """
        # [변경] 플레이어 초기 스탯 설정 - player_vars로 이동
        # player_state의 player_vars가 플레이어 스탯을 관리함

        # 시작 위치 설정
        start_scene_id = scenario_data.get('start_scene_id')
        if start_scene_id:
            self.location = start_scene_id
            # 🔴 중요: narrative_history가 완전히 비어있을 때만 시작 메시지 기록
            # (세션 로드 시 중복 방지)
            if not self.narrative_history:
                self.add_narrative_event(f"게임이 '{start_scene_id}'에서 시작되었습니다.")
                logger.info(f"🎮 [GAME START] Initial start event recorded at '{start_scene_id}'")

        # NPC 초기화
        npcs_data = scenario_data.get('npcs', [])
        scenes_data = scenario_data.get('scenes', [])

        for npc in npcs_data:
            npc_name = npc.get('name')
            if not npc_name:
                continue

            # NPC 위치 찾기
            npc_location = None
            for scene in scenes_data:
                scene_npcs = scene.get('npcs', [])
                scene_enemies = scene.get('enemies', [])
                if npc_name in scene_npcs or npc_name in scene_enemies:
                    npc_location = scene.get('scene_id')
                    break

            # 🔴 FIX: HP 값을 정수로 강제 변환 (문자열 방지)
            npc_hp_raw = npc.get('hp', 100)
            npc_max_hp_raw = npc.get('max_hp', npc_hp_raw)

            try:
                npc_hp = int(npc_hp_raw)
            except (ValueError, TypeError):
                logger.warning(f"Invalid HP value for NPC '{npc_name}': {npc_hp_raw}, using default 100")
                npc_hp = 100

            try:
                npc_max_hp = int(npc_max_hp_raw)
            except (ValueError, TypeError):
                logger.warning(f"Invalid max_hp value for NPC '{npc_name}': {npc_max_hp_raw}, using HP value {npc_hp}")
                npc_max_hp = npc_hp

            # NPC 초기 상태 설정
            self.npcs[npc_name] = {
                "status": "alive",
                "hp": npc_hp,
                "max_hp": npc_max_hp,
                "emotion": "neutral",
                "relationship": 50,
                "is_hostile": npc.get('isEnemy', False),
                "location": npc_location or "unknown",
                "flags": {}
            }

        logger.info(f"🌍 [WORLD STATE] Initialized with {len(self.npcs)} NPCs")

    # ========================================
    # 2. 상태 업데이트 (핵심 로직)
    # ========================================

    def update_state(self, effect_data: Union[Dict[str, Any], List[Dict[str, Any]]]):
        """
        효과 데이터를 받아 상태를 업데이트 (순수 규칙 기반, LLM 개입 없음)

        Args:
            effect_data: 효과 데이터(단일 dict 또는 list)
                예시: {"hp": -10, "gold": +5, "item_add": "물약"}
                      [{"hp": -10}, {"npc": "마인 J", "relationship": +10}]

        지원 효과:
        - hp, gold, sanity, radiation 등 수치 증감
        - item_add, item_remove: 아이템 추가/제거
        - npc: NPC 이름과 함께 relationship, emotion, status, flags 변경
        - global_flag: 지역 플래그 설정
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
                    old_value = self.player.get(stat, 0)
                    self._update_player_stat(stat, effect[stat])
                    new_value = self.player.get(stat, 0)
                    if old_value != new_value:
                        self.add_narrative_event(f"플레이어의 {stat.upper()}이 {old_value}에서 {new_value}로 변경되었습니다.")

            # 커스텀 스탯 변경
            for key, value in effect.items():
                if key in self.player["custom_stats"]:
                    old_value = self.player["custom_stats"].get(key, 0)
                    self._update_player_stat(key, value, is_custom=True)
                    new_value = self.player["custom_stats"].get(key, 0)
                    if old_value != new_value:
                        self.add_narrative_event(f"플레이어의 {key}이(가) {old_value}에서 {new_value}로 변경되었습니다.")

            # 아이템 관리
            if "item_add" in effect:
                item_name = effect["item_add"]
                self._add_item(item_name)
                self.add_narrative_event(f"플레이어가 '{item_name}'을(를) 획득했습니다.")
            if "item_remove" in effect:
                item_name = effect["item_remove"]
                self._remove_item(item_name)
                self.add_narrative_event(f"플레이어가 '{item_name}'을(를) 사용/잃었습니다.")

            # NPC 관계 변경
            if "npc" in effect:
                npc_name = effect["npc"]
                self._update_npc_state(npc_name, effect)

            # 글로벌 플래그
            if "global_flag" in effect:
                flag_name = effect["global_flag"]
                flag_value = effect.get("value", True)
                old_flag_value = self.global_flags.get(flag_name, False)
                self.global_flags[flag_name] = flag_value

                # 플래그 변경 시 자동으로 서사 이벤트 추가
                if old_flag_value != flag_value:
                    self.add_narrative_event(f"팩트: [{flag_name}]이(가) [{flag_value}]로 변경되었습니다.")

            # 퀘스트 관리
            if "quest_start" in effect:
                quest_id = effect["quest_start"]
                self.player["quests"][quest_id] = "active"
                self.add_narrative_event(f"퀘스트 '{quest_id}'이(가) 시작되었습니다.")
            if "quest_complete" in effect:
                quest_id = effect["quest_complete"]
                self.player["quests"][quest_id] = "completed"
                self.add_narrative_event(f"퀘스트 '{quest_id}'을(를) 완료했습니다.")
            if "quest_fail" in effect:
                quest_id = effect["quest_fail"]
                self.player["quests"][quest_id] = "failed"
                self.add_narrative_event(f"퀘스트 '{quest_id}'이(가) 실패했습니다.")

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
        """
        아이템 추가 (레지스트리 기반)
        레지스트리에 등록된 아이템인지 확인하고, 경고 로그 출력
        """
        if isinstance(item, str):
            if item not in self.player["inventory"]:
                self.player["inventory"].append(item)
                # 레지스트리 확인 및 로깅
                if item in self.item_registry:
                    item_info = self.item_registry[item]
                    logger.info(f"✅ [INVENTORY] Added item '{item}' (registered: {item_info.get('description', 'No description')})")
                else:
                    logger.warning(f"⚠️ [INVENTORY] Added item '{item}' but it's NOT in item_registry (레지스트리에 미등록)")
        elif isinstance(item, list):
            for i in item:
                if i not in self.player["inventory"]:
                    self.player["inventory"].append(i)
                    if i in self.item_registry:
                        item_info = self.item_registry[i]
                        logger.info(f"✅ [INVENTORY] Added item '{i}' (registered: {item_info.get('description', 'No description')})")
                    else:
                        logger.warning(f"⚠️ [INVENTORY] Added item '{i}' but it's NOT in item_registry")

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
        changes = []

        # 관계도 변경
        if "relationship" in effect:
            delta = effect["relationship"]
            if isinstance(delta, (int, float)):
                old_rel = npc["relationship"]
                npc["relationship"] += delta
                npc["relationship"] = max(0, min(100, npc["relationship"]))
                changes.append(f"관계도 {old_rel} → {npc['relationship']}")

        # 감정 변경
        if "emotion" in effect:
            old_emotion = npc["emotion"]
            npc["emotion"] = effect["emotion"]
            if old_emotion != npc["emotion"]:
                changes.append(f"감정 {old_emotion} → {npc['emotion']}")

        # 생존 여부
        if "status" in effect:
            old_status = npc["status"]
            npc["status"] = effect["status"]
            if old_status != npc["status"]:
                changes.append(f"상태 {old_status} → {npc['status']}")

        # NPC 개별 플래그
        if "npc_flag" in effect:
            flag_name = effect["npc_flag"]
            flag_value = effect.get("flag_value", True)
            npc["flags"][flag_name] = flag_value
            changes.append(f"플래그 '{flag_name}' = {flag_value}")

        # HP 변경(적용 예: {"npc": "마인 J", "hp": -10})
        if "hp" in effect:
            hp_change = effect["hp"]
            if isinstance(hp_change, (int, float)):
                old_hp = npc.get("hp", 100)
                npc["hp"] = npc.get("hp", 100) + hp_change
                npc["hp"] = max(0, min(npc["hp"], npc.get("max_hp", 100)))
                changes.append(f"HP {old_hp} → {npc['hp']}")

        # 위치 변경(적용 예: {"npc": "마인 J", "location": "오리 집"})
        if "location" in effect:
            old_loc = npc.get("location", "unknown")
            npc["location"] = effect["location"]
            changes.append(f"위치 {old_loc} → {npc['location']}")

        # 변경사항이 있으면 서사 이벤트 추가
        if changes:
            change_text = ", ".join(changes)
            self.add_narrative_event(f"NPC '{npc_name}': {change_text}")

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

    def get_npc_state(self, npc_name: str) -> Optional[Dict[str, Any]]:
        """NPC 상태 조회"""
        return self.npcs.get(npc_name)

    def set_npc_state(self, npc_name: str, state_data: Dict[str, Any]):
        """NPC 상태 설정"""
        if npc_name in self.npcs:
            self.npcs[npc_name].update(state_data)
        else:
            self.npcs[npc_name] = state_data

    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환 (직렬화)"""
        return {
            "time": self.time,
            "location": self.location,
            "global_flags": self.global_flags,
            "turn_count": self.turn_count,
            "npcs": self.npcs,
            "history": self.history,
            "narrative_history": self.narrative_history,
            "player": self.player,  # player 데이터도 직렬화
            "item_registry": self.item_registry  # [추가] 아이템 레지스트리 직렬화
        }

    def from_dict(self, data: Dict[str, Any]):
        """딕셔너리에서 복원 (역직렬화) - 역호환성 유지"""
        self.time = data.get("time", {"day": 1, "phase": "morning"})
        self.location = data.get("location")
        self.global_flags = data.get("global_flags", {})
        self.turn_count = data.get("turn_count", 0)
        self.npcs = data.get("npcs", {})
        self.history = data.get("history", [])
        self.narrative_history = data.get("narrative_history", [])

        # ✅ 작업 1: player 데이터 병합 - 기존 데이터 유지하며 업데이트
        if "player" in data:
            saved_player = data["player"]
            # 기존 self.player의 구조를 유지하면서 저장된 값으로 업데이트
            self.player.update(saved_player)

            # [역호환성] 기존 문자열 리스트 인벤토리 데이터 안전하게 처리
            if "inventory" in saved_player:
                inventory_data = saved_player["inventory"]
                if isinstance(inventory_data, list):
                    # 모든 인벤토리 항목을 문자열로 변환 (안전성)
                    self.player["inventory"] = [str(item) for item in inventory_data]
                    logger.info(f"📦 [INVENTORY] Loaded {len(self.player['inventory'])} items from saved data")

    def _get_snapshot(self) -> Dict[str, Any]:
        """현재 상태 스냅샷 (히스토리용)"""
        return {
            "player_hp": self.player.get("hp"),
            "player_gold": self.player.get("gold"),
            "location": self.location
        }

    # ========================================
    # 4. NPC HP 관리 및 불사신 방지 (핵심 로직)
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
        # 🔴 FIX: amount를 정수로 강제 변환
        try:
            amount = int(amount)
        except (ValueError, TypeError):
            logger.error(f"Invalid amount type for update_npc_hp: {type(amount).__name__} = {amount}, using 0")
            amount = 0

        # NPC가 없으면 초기화
        if npc_id not in self.npcs:
            logger.warning(f"NPC '{npc_id}' not found. Initializing with default values.")
            self.npcs[npc_id] = {
                "status": "alive",
                "hp": 100,
                "max_hp": 100,
                "emotion": "neutral",
                "relationship": 50,
                "flags": {}
            }

        npc = self.npcs[npc_id]

        # 이미 죽은 NPC는 더 이상 HP 변경 불가
        if npc.get("status") == "dead":
            logger.warning(f"NPC '{npc_id}' is already dead. Cannot change HP.")
            return {
                "npc_id": npc_id,
                "hp": 0,
                "status": "dead",
                "is_dead": True,
                "message": f"{npc_id}는 이미 죽었습니다."
            }

        # 🔴 FIX: HP 값을 정수로 강제 변환
        old_hp_raw = npc.get("hp", 100)
        max_hp_raw = npc.get("max_hp", 100)

        try:
            old_hp = int(old_hp_raw)
        except (ValueError, TypeError):
            logger.warning(f"Invalid HP type for NPC '{npc_id}': {type(old_hp_raw).__name__} = {old_hp_raw}, using 100")
            old_hp = 100

        try:
            max_hp = int(max_hp_raw)
        except (ValueError, TypeError):
            logger.warning(f"Invalid max_hp type for NPC '{npc_id}': {type(max_hp_raw).__name__} = {max_hp_raw}, using 100")
            max_hp = 100

        # HP 변경 (순수 정수 연산)
        new_hp = old_hp + amount

        # HP 범위 제한 (0 ~ max_hp)
        new_hp = max(0, min(new_hp, max_hp))
        npc["hp"] = new_hp
        npc["max_hp"] = max_hp  # max_hp도 정수로 보장

        # 🔴 사망 판정 (규칙 기반 - LLM 개입 불가)
        is_dead = False
        if new_hp <= 0:
            npc["status"] = "dead"
            is_dead = True
            logger.info(f"🪦 [DEATH] NPC '{npc_id}' has died. HP: {old_hp} -> 0")
        elif npc.get("status") == "dead":
            # 혹시 모를 불일치 방지: HP가 0인데 status가 alive인 경우 강제 수정
            npc["status"] = "dead"
            is_dead = True

        return {
            "npc_id": npc_id,
            "hp": new_hp,
            "old_hp": old_hp,
            "status": npc["status"],
            "is_dead": is_dead,
            "message": f"{npc_id}의 HP: {old_hp} -> {new_hp}" + (" (사망)" if is_dead else "")
        }

    def increment_turn(self):
        """턴 카운트 증가"""
        self.turn_count += 1

    # ========================================
    # 5. 전투 시스템 (Combat System)
    # ========================================

    def find_npc_key(self, query_name: str) -> Optional[str]:
        """
        부분 명칭으로 NPC 키를 찾는 유틸리티

        Args:
            query_name: 유저가 입력한 NPC 명칭 (예: "노인", "마인")

        Returns:
            매칭된 NPC 키 (예: "노인 J") 또는 None
        """
        if not query_name:
            return None

        query_lower = query_name.lower().replace(" ", "")

        # 1. 정확한 일치 확인
        for npc_key in self.npcs.keys():
            if npc_key.lower().replace(" ", "") == query_lower:
                logger.info(f"🎯 [NPC MATCH] Exact match: '{query_name}' -> '{npc_key}'")
                return npc_key

        # 2. 부분 일치 확인 (query가 npc_key에 포함)
        for npc_key in self.npcs.keys():
            npc_key_normalized = npc_key.lower().replace(" ", "")
            if query_lower in npc_key_normalized or npc_key_normalized in query_lower:
                logger.info(f"🎯 [NPC MATCH] Partial match: '{query_name}' -> '{npc_key}'")
                return npc_key

        # 3. 유사도 기반 매칭 (difflib)
        best_match = None
        best_ratio = 0.0

        for npc_key in self.npcs.keys():
            npc_key_normalized = npc_key.lower().replace(" ", "")
            ratio = difflib.SequenceMatcher(None, query_lower, npc_key_normalized).ratio()

            if ratio > best_ratio and ratio >= 0.6:  # 60% 이상 유사도
                best_ratio = ratio
                best_match = npc_key

        if best_match:
            logger.info(f"🎯 [NPC MATCH] Fuzzy match ({best_ratio:.2f}): '{query_name}' -> '{best_match}'")
            return best_match

        logger.warning(f"❌ [NPC MATCH] No match found for: '{query_name}'")
        return None

    def damage_npc(self, npc_name: str, amount: int) -> str:
        """
        NPC에게 데미지를 가하고 HP를 차감하며, 사망 처리를 수행

        Args:
            npc_name: NPC 명칭 (부분 명칭 가능, find_npc_key로 자동 매칭)
            amount: 데미지 양 (양수)

        Returns:
            전투 결과 텍스트 (예: "노인 J에게 4 피해! (HP 10 -> 6)")
        """
        import random

        # NPC 키 찾기
        npc_key = self.find_npc_key(npc_name)

        if not npc_key:
            error_msg = f"⚠️ 공격 대상 '{npc_name}'을(를) 찾을 수 없습니다."
            logger.warning(f"[COMBAT] {error_msg}")
            return error_msg

        # NPC 데이터 방어적 초기화
        if npc_key not in self.npcs:
            self.npcs[npc_key] = {
                "status": "alive",
                "hp": 10,
                "max_hp": 10,
                "emotion": "neutral",
                "relationship": 50,
                "is_hostile": False,
                "flags": {}
            }

        npc = self.npcs[npc_key]

        # HP 필드 방어
        if "hp" not in npc:
            npc["hp"] = 10
        if "max_hp" not in npc:
            npc["max_hp"] = npc["hp"]
        if "status" not in npc:
            npc["status"] = "alive"
        if "is_hostile" not in npc:
            npc["is_hostile"] = False

        # 이미 죽은 NPC는 공격 불가
        if npc.get("status") == "dead":
            dead_msg = f"{npc_key}는 이미 쓰러져 차갑게 식었습니다."
            logger.info(f"[COMBAT] {dead_msg}")
            return dead_msg

        # ========================================
        # 💥 작업 1: 공격받은 NPC의 감정/관계 즉시 변경
        # ========================================
        npc["relationship"] = 0  # 관계도 0으로 고정
        npc["emotion"] = "hostile"  # 감정 hostile로 변경
        npc["is_hostile"] = True  # 적대 상태로 전환
        logger.info(f"💢 [COMBAT] {npc_key} is now hostile (relationship=0, emotion=hostile)")

        # 데미지 적용
        old_hp = int(npc["hp"])
        new_hp = max(0, old_hp - amount)
        npc["hp"] = new_hp

        result_text = f"{npc_key}에게 {amount} 피해! (HP {old_hp} -> {new_hp})"

        # 사망 판정
        if new_hp <= 0:
            npc["status"] = "dead"
            npc["hp"] = 0
            result_text += f"\n💀 {npc_key}는 쓰러져 죽었습니다."
            logger.info(f"🪦 [COMBAT] {npc_key} has been killed. HP: {old_hp} -> 0")
        else:
            # ========================================
            # 💥 작업 2: NPC 반격 로직 (살아있을 때만)
            # ========================================
            # 70% 확률로 반격
            if random.random() < 0.7:
                counter_damage = random.randint(5, 15)

                # 플레이어 HP 감소
                player_hp = self.player.get("hp", 100)
                new_player_hp = max(0, player_hp - counter_damage)
                self.player["hp"] = new_player_hp

                result_text += f"\n⚔️ {npc_key}의 반격! 플레이어가 {counter_damage} 피해를 입었습니다! (남은 HP: {new_player_hp})"
                logger.info(f"💥 [COUNTER ATTACK] {npc_key} counter-attacked player: {counter_damage} damage (Player HP: {player_hp} -> {new_player_hp})")
                logger.info(f"[SYNC CHECK] Player HP synced: {new_player_hp}")

                # 플레이어 사망 체크
                if new_player_hp <= 0:
                    result_text += "\n💀 당신은 치명상을 입고 쓰러졌습니다..."
                    logger.critical(f"💀 [PLAYER DEATH] Player HP reached 0")

                    # 서사 이벤트 기록
                    self.add_narrative_event(f"{npc_key}의 반격으로 플레이어 사망")

        logger.info(f"[COMBAT] {npc_key} damaged: {old_hp} -> {new_hp}, status={npc['status']}")

        return result_text

    def record_combat_event(self, text: str):
        """
        전투 이벤트를 narrative_history에 기록

        Args:
            text: 전투 이벤트 설명
        """
        self.add_narrative_event(text)
        logger.info(f"⚔️ [COMBAT EVENT] {text}")

    # ✅ 작업 3: 플레이어 HP 반격 로직을 위한 메서드 추가
    def apply_player_damage(self, amount: int):
        """
        플레이어에게 직접 데미지를 가함 (반격용)

        Args:
            amount: 데미지 양 (양수)
        """
        try:
            amount = int(amount)
        except (ValueError, TypeError):
            logger.error(f"Invalid damage amount: {amount}, using 0")
            amount = 0

        old_hp = self.player.get("hp", 100)
        new_hp = max(0, old_hp - amount)
        self.player["hp"] = new_hp

        logger.info(f"💥 [PLAYER DAMAGE] Player HP: {old_hp} -> {new_hp}")

        return new_hp

    # ========================================
    # 6. LLM 컨텍스트 생성 (get_llm_context)
    # ========================================

    def get_llm_context(self) -> str:
        """
        현재 LLM 프롬프트에 주입할 단단한 진실 컨텍스트
        - 플레이어 현재 스탯
        - NPC 생존 상태
        - 최근 서사 이벤트 (최근 5개)

        LLM은 이 정보를 토대로 무시할 수 없으며
        서사 생성 시 반드시 이 데이터를 기준으로 작성해야 함
        """
        lines = ["=== 🌍 WORLD STATE (단단한 진실) ===\n"]

        # 플레이어 상태
        lines.append("[플레이어 상태]")
        lines.append(f"- HP: {self.player['hp']}/{self.player['max_hp']}")

        if self.player.get('gold', 0) > 0:
            lines.append(f"- 골드: {self.player['gold']}")

        for key, value in self.player.get("custom_stats", {}).items():
            lines.append(f"- {key}: {value}")

        if self.player["inventory"]:
            lines.append(f"- 보유중: {', '.join(self.player['inventory'])}")
        else:
            lines.append("- 보유중: 없음")

        # NPC 생존 상태 (핵심만 표시 - 환각 방지)
        if self.npcs:
            lines.append("\n[NPC/적 상태]")
            for npc_name, npc_data in self.npcs.items():
                status = npc_data.get("status", "alive")
                hp_raw = npc_data.get("hp", 100)

                # ✅ 작업 3: HP 값을 정수로 강제 변환 (타입 에러 방지)
                try:
                    hp = int(float(hp_raw))
                except (ValueError, TypeError):
                    logger.warning(f"Invalid HP value for NPC '{npc_name}': {hp_raw}, using default 100")
                    hp = 100

                if status == "dead":
                    lines.append(f"- {npc_name}: 전투 사망 (HP: 0) - 더이상 무력/불가능")
                elif hp <= 0:
                    lines.append(f"- {npc_name}: 전투 사망 (HP: 0) - 더이상 무력/불가능")
                else:
                    lines.append(f"- {npc_name}: 생존 (HP: {hp})")

        # 최근 서사 이벤트 (최근 5개)
        if self.narrative_history:
            lines.append("\n[최근 사건 요약]")
            recent_events = self.narrative_history[-5:]
            for i, event in enumerate(recent_events, 1):
                lines.append(f"{i}. {event}")

        return "\n".join(lines)
