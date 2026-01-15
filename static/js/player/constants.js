// constants.js - 상수 및 전역 변수 관리

// 서버 상태는 무시하고 항상 초기화된 상태로 시작
const serverHasState = false;  // 항상 false로 설정하여 서버 상태 무시

// 전역 상태 변수
let isGameEnded = false;
let isScenarioLoaded = false;
let isInternalNavigation = false;  // 내부 네비게이션 플래그
let hasGameStarted = false;  // 게임이 시작되었는지 (채팅 내역이 있는지)
let isStreaming = false;  // 스트리밍 중 여부 추가
let responseTimerInterval = null;  // 응답 시간 타이머
let responseStartTime = null;  // 응답 시작 시간
let currentSessionKey = '';  // 현재 세션 키 저장
let currentSessionId = sessionStorage.getItem("current_session_id") || null;  // 세션 ID 유지 - sessionStorage에서 복원
let currentScenarioId = null;  // 현재 로드된 시나리오 ID 저장

// 상수 정의
const CHAT_LOG_KEY = 'trpg_chat_log';
const SCENARIO_LOADED_KEY = 'trpg_scenario_loaded';
const CURRENT_SCENARIO_KEY = 'trpg_current_scenario';
const CURRENT_SCENARIO_ID_KEY = 'trpg_scenario_id';
const SESSION_KEY_STORAGE = 'trpg_session_key';
const MODEL_PROVIDER_KEY = 'trpg_model_provider';
const MODEL_VERSION_KEY = 'trpg_model_version';
const DEBUG_MODE_KEY = 'trpg_debug_mode';
const GAME_ENDED_KEY = 'trpg_game_ended';
const NAVIGATION_FLAG_KEY = 'trpg_navigation_flag';

// 새로고침 감지 및 경고
window.addEventListener('beforeunload', function(e) {
    // 스트리밍 중이면 무조건 경고
    if (isStreaming) {
        e.preventDefault();
        e.returnValue = 'AI가 답변을 생성하고 있습니다. 페이지를 벗어나시겠습니까?';
        return e.returnValue;
    }

    // 내부 네비게이션이면 경고 안 함
    if (isInternalNavigation) {
        // 내부 네비게이션 플래그 설정 (다음 페이지 로드 시 복원용)
        sessionStorage.setItem(NAVIGATION_FLAG_KEY, 'true');
        return;
    }

    // 게임이 진행 중이면 경고 (채팅 로그가 있고 게임이 시작됨)
    if (hasGameStarted && isScenarioLoaded) {
        e.preventDefault();
        e.returnValue = '페이지를 벗어나면 현재 진행 내역이 초기화됩니다. 계속하시겠습니까?';
        return e.returnValue;
    }
});

// 페이지 로드 시 상태 복원 또는 초기화
(function() {
    // 새로고침(F5) vs 내부 네비게이션 구분
    const isPageRefresh = performance.navigation.type === 1 ||
                         (performance.getEntriesByType('navigation')[0]?.type === 'reload');

    // 내부 네비게이션으로 돌아온 경우 (전체 씬 보기 -> 플레이어 모드)
    const isReturningFromNavigation = sessionStorage.getItem(NAVIGATION_FLAG_KEY) === 'true';
    sessionStorage.removeItem(NAVIGATION_FLAG_KEY);  // 플래그 제거

    // 새로고침이면 무조건 초기화
    if (isPageRefresh) {
        console.log('🔄 새로고침 감지 - 게임 상태 초기화');
        clearAllGameState();
        initializeEmptyGameUI();
        return;
    }

    // 저장된 게임 상태가 있는지 확인
    const hasSavedGame = sessionStorage.getItem(CHAT_LOG_KEY) || sessionStorage.getItem(SCENARIO_LOADED_KEY);

    // 내부 네비게이션으로 돌아왔거나 저장된 게임이 있으면 복원
    if (isReturningFromNavigation && hasSavedGame) {
        console.log('🔄 내부 네비게이션 복귀 - 게임 상태 복원 중...');
        // 복원은 DOMContentLoaded에서 restoreChatLog()가 처리
        return;
    }

    // 완전히 새로운 시작 (첫 방문)
    console.log('🆕 새로운 게임 세션 시작');
    clearAllGameState();
    initializeEmptyGameUI();
})();

// UI 초기화 함수
function initializeEmptyGameUI() {
    const chatLog = document.getElementById('chat-log');
    const initResult = document.getElementById('init-result');
    const aiLoading = document.getElementById('ai-loading');

    if (chatLog && initResult && aiLoading) {
        // 초기 메시지만 남기고 모두 제거
        chatLog.innerHTML = '';
        chatLog.appendChild(initResult);

        // 초기 안내 메시지 복원
        const introHtml = `
            <div id="intro-message" class="flex gap-4 fade-in mb-4">
                <div class="w-8 h-8 rounded-lg bg-indigo-900 flex items-center justify-center shrink-0">
                    <i data-lucide="bot" class="text-white w-4 h-4"></i>
                </div>
                <div class="flex-1">
                    <div class="text-indigo-400 text-xs font-bold mb-1">GM</div>
                    <div class="bg-[#1a1a1e] border-gray-700 p-3 rounded-lg border text-gray-300 text-sm leading-relaxed">
                        시스템에 접속했습니다. 우측 상단의 <span class="text-indigo-400 font-bold">[시나리오 불러오기]</span> 버튼을 눌러 게임을 로드하세요.
                    </div>
                </div>
            </div>
        `;
        initResult.insertAdjacentHTML('afterend', introHtml);
        chatLog.appendChild(aiLoading);

        // 스탯 영역 초기화
        const statsArea = document.getElementById('player-stats-area');
        if (statsArea) {
            statsArea.innerHTML = `
                <div class="text-gray-500 text-sm text-center py-4 bg-gray-800/50 rounded-lg border border-gray-700 border-dashed">
                    <i data-lucide="ghost" class="w-6 h-6 mx-auto mb-2 opacity-50"></i>
                    데이터 없음<br>
                    <span class="text-xs">상단 [시나리오 불러오기]를 눌러주세요.</span>
                </div>
            `;
        }

        // 디버그 영역 초기화 (NPC Status, World State)
        const npcStatusArea = document.getElementById('npc-status-area');
        if (npcStatusArea) {
            npcStatusArea.innerHTML = `
                <div class="text-gray-500 text-xs text-center py-2 bg-gray-800/50 rounded border border-gray-700 border-dashed">
                    NPC 데이터 없음
                </div>
            `;
        }

        const worldStateArea = document.getElementById('world-state-area');
        if (worldStateArea) {
            worldStateArea.innerHTML = `
                <div class="text-gray-500 text-xs text-center py-2 bg-gray-800/50 rounded border border-gray-700 border-dashed">
                    World State 데이터 없음
                </div>
            `;
        }

        // 세션 키 초기화
        currentSessionKey = '';
        localStorage.removeItem(SESSION_KEY_STORAGE);

        // UI 비활성화
        disableGameUI();
    }
}

// 모든 게임 상태 초기화 함수
function clearAllGameState() {
    sessionStorage.removeItem(CHAT_LOG_KEY);
    sessionStorage.removeItem(SCENARIO_LOADED_KEY);
    sessionStorage.removeItem(CURRENT_SCENARIO_KEY);
    sessionStorage.removeItem('trpg_session_key');
    sessionStorage.removeItem(GAME_ENDED_KEY);
    sessionStorage.removeItem('trpg_world_state');
    sessionStorage.removeItem('trpg_player_stats');
    localStorage.removeItem(SESSION_KEY_STORAGE);

    // 메모리 변수도 초기화
    currentSessionId = null;
    currentSessionKey = '';

    console.log('🧹 All game state cleared (including session ID)');
}

// 외부에서 접근 가능하도록 함수들을 window 객체에 할당
window.initializeEmptyGameUI = initializeEmptyGameUI;
window.clearAllGameState = clearAllGameState;

