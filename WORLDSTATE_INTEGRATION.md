# 🛠️ WorldState Manager 통합 완료

## 개요
LLM 환각(Hallucination) 방지 및 정합성 유지를 위한 규칙 기반 상태 관리 시스템 구축 완료.

---

## 주요 변경사항

### 1. `core/state.py` - WorldStateManager 구현
- **규칙 기반 상태 관리**: 모든 수치 계산은 Python 정수 연산으로만 처리 (LLM 개입 배제)
- **불사신 방지 로직**: `update_npc_hp()` 메서드에서 HP가 0 이하면 즉시 `status = "dead"` 강제 설정
- **LLM 컨텍스트 생성**: `get_llm_context()` 메서드로 절대적 진실을 프롬프트에 주입
- **데이터 구조**:
  - `Player`: HP, Sanity, Inventory, Custom Stats
  - `NPCs`: HP, Status (alive/dead), Relationship, Flags
  - `World`: Time, Location, Global Flags, Turn Count

### 2. `schemas.py` - PlayerState 확장
- `world_state: Optional[Dict[str, Any]]` 필드 추가
- WorldStateManager의 스냅샷을 PlayerState에 저장

### 3. `game_engine.py` - 엔진 통합
- **rule_node**: WorldState를 통한 효과 적용 (규칙 기반)
  - Effect 처리: HP, Gold, Item 등 모든 수치 변경을 WorldState가 처리
  - 레거시 `player_vars`와 동기화 (하위 호환성 유지)
- **턴 추적**: `world_state.increment_turn()` 호출

### 4. `routes/game.py` - DB 영속성 관리
- **세션 저장**: `save_game_session()` - 매 턴마다 WorldState를 PostgreSQL에 저장
- **세션 복원**: `load_game_session()` - 세션 키로 이전 진행 상황 복원
- **세션 키 전송**: 클라이언트가 다음 요청에서 사용할 수 있도록 SSE로 전송
- **휘발성 제거**: Flask 세션 대신 DB 저장 방식 채택

### 5. `models.py` - GameSession 테이블 추가
```python
class GameSession(Base):
    - session_key: 고유 세션 식별자 (UUID)
    - user_id: 유저 ID (비로그인은 NULL)
    - scenario_id: 시나리오 ID
    - player_state: PlayerState 전체 (JSONB)
    - world_state: WorldState 스냅샷 (JSONB)
    - current_scene_id, turn_count: 메타 정보
    - created_at, updated_at, last_played_at: 타임스탬프
```

**Railway PostgreSQL 최적화**:
- JSONB 타입 사용 (쿼리 성능 향상)
- 인덱스 설정 (session_key, user_id, scenario_id, last_played_at)
- 연결 풀링 설정 (pool_size=10, pool_recycle=3600)
- 오래된 세션 자동 정리 함수 (`cleanup_old_sessions()`)

### 6. `config/prompt_player.yaml` - 프롬프트 최적화
```yaml
scene_description: |
  **🔴 CRITICAL: World State에서 제공하는 수치(HP, 생사 여부, 인벤토리)는 
  절대적 진실입니다. 이를 기반으로만 서사를 작성하세요.**

battle_action: |
  **🔴 CRITICAL: World State에서 제공하는 NPC/적의 HP와 생사 여부는 
  절대적 진실입니다. 절대로 죽은 적을 살려내거나, HP 수치를 무시하지 마세요.**
```

---

## Railway 배포 체크리스트

### 1. 환경 변수 설정
Railway 대시보드에서 다음 환경 변수 설정:
```bash
DATABASE_URL=postgresql://user:password@host:port/dbname  # Railway가 자동 제공
OPENROUTER_API_KEY=your_api_key_here
```

### 2. 데이터베이스 초기화
```bash
# Railway 콘솔에서 실행
python init_db.py
```

또는 `app.py`에서 자동 실행:
```python
from models import create_tables

@app.on_event("startup")
async def startup_event():
    create_tables()
```

### 3. 주기적 세션 정리 (선택사항)
Railway Cron Job 설정:
```bash
# 매일 오전 3시에 7일 이상 오래된 세션 삭제
0 3 * * * python -c "from models import cleanup_old_sessions; cleanup_old_sessions(7)"
```

---

## 사용 방법

### 클라이언트 측 (JavaScript)
```javascript
let sessionKey = null;

// 게임 시작
fetch('/game/act_stream', {
    method: 'POST',
    body: new FormData({
        action: '시작',
        model: 'openai/gpt-4',
        session_key: sessionKey  // 첫 시작은 null
    })
});

// SSE 응답 처리
eventSource.onmessage = (event) => {
    const data = JSON.parse(event.data);
    
    if (data.type === 'session_key') {
        sessionKey = data.content;  // 저장해서 다음 요청에 사용
        localStorage.setItem('game_session', sessionKey);
    }
};

// 다음 턴
fetch('/game/act_stream', {
    method: 'POST',
    body: new FormData({
        action: '문을 연다',
        session_key: sessionKey  // 이전에 받은 세션 키 전송
    })
});
```

### 서버 측 (Python)
```python
# WorldState 직접 사용 예시
from core.state import WorldState

world_state = WorldState()

# NPC HP 감소 (불사신 방지)
result = world_state.update_npc_hp("노인 J", -50)
if result['is_dead']:
    print(f"{result['message']}")  # "노인 J의 HP: 50 -> 0 (사망)"

# LLM 프롬프트에 주입
context = world_state.get_llm_context()
# "=== 🔴 WORLD STATE (절대적 진실) ===
#  [NPC/적 상태]
#  - 노인 J: ☠️ 사망 (HP: 0) ← 절대 부활 불가"
```

---

## 핵심 원칙

1. **모든 수치 계산은 Python 코드로만 처리** (LLM 배제)
2. **HP가 0이면 status = "dead" 강제 설정** (불사신 방지)
3. **WorldState는 절대적 진실** (LLM은 이를 읽기만 가능)
4. **매 턴마다 DB 저장** (휘발성 제거)
5. **세션 키로 진행 상황 복원** (유저 경험 향상)

---

## 테스트 방법

```bash
# 로컬 테스트
python init_db.py

# 서버 시작
python app.py

# Railway 배포 후 테스트
curl -X POST https://your-app.railway.app/game/act_stream \
  -F "action=시작" \
  -F "model=openai/gpt-4"
```

---

## 문제 해결

### PostgreSQL 연결 오류
```bash
# Railway 환경 변수 확인
echo $DATABASE_URL

# 테이블 재생성
python init_db.py
```

### 세션 복원 실패
```python
# 세션 키 유효성 확인
from models import SessionLocal, GameSession

db = SessionLocal()
session = db.query(GameSession).filter_by(session_key='your-key').first()
print(session.to_dict() if session else "Not found")
```

---

## 향후 개선사항

1. **Redis 캐싱**: 자주 접근하는 세션을 Redis에 캐시
2. **백그라운드 정리**: Celery로 세션 정리 작업 스케줄링
3. **WorldState 검증**: 각 턴마다 상태 일관성 체크
4. **롤백 기능**: 이전 턴으로 되돌리기 (history 활용)

---

✅ WorldState Manager 통합 완료!

