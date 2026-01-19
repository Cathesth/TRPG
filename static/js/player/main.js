// main.js - DOMContentLoaded 이벤트 및 초기화

document.addEventListener('DOMContentLoaded', function() {
    // 아이콘 초기화
    lucide.createIcons();

    // ✅ [최우선 순위] URL 파라미터에서 session_id와 scenario_id 추출하여 sessionStorage 강제 업데이트
    const urlParams = new URLSearchParams(window.location.search);
    const urlSessionId = urlParams.get('session_id');
    const urlScenarioId = urlParams.get('scenario_id');

    // ✅ [FIX] URL에 session_id가 있으면 무조건 sessionStorage 덮어쓰기
    if (urlSessionId) {
        console.log('🔑 [INIT] URL parameter detected (priority #1), forcing update:', urlSessionId);
        currentSessionId = urlSessionId;
        sessionStorage.setItem('trpg_session_key', urlSessionId);

        // 레거시 키도 동기화
        if (CURRENT_SESSION_ID_KEY && CURRENT_SESSION_ID_KEY !== 'trpg_session_key') {
            sessionStorage.setItem(CURRENT_SESSION_ID_KEY, urlSessionId);
        }
        console.log('✅ [INIT] Session ID from URL saved to storage (overriding old values)');
    } else {
        // URL에 없으면 sessionStorage에서 복원
        const storageSessionId = sessionStorage.getItem('trpg_session_key');
        if (storageSessionId) {
            currentSessionId = storageSessionId;
            console.log('🔑 [INIT] Session ID restored from storage (priority #2):', currentSessionId);
        } else if (!currentSessionId) {
            currentSessionId = sessionStorage.getItem(CURRENT_SESSION_ID_KEY);
            if (currentSessionId) {
                console.log('🔑 [INIT] Session ID restored from legacy key:', currentSessionId);
            }
        }
    }

    // ✅ [FIX] URL에 scenario_id가 있으면 무조건 sessionStorage 덮어쓰기
    if (urlScenarioId) {
        console.log('📋 [INIT] URL scenario_id detected, forcing update:', urlScenarioId);
        currentScenarioId = urlScenarioId;
        sessionStorage.setItem('trpg_scenario_id', urlScenarioId);

        // 레거시 키도 동기화
        if (CURRENT_SCENARIO_ID_KEY && CURRENT_SCENARIO_ID_KEY !== 'trpg_scenario_id') {
            sessionStorage.setItem(CURRENT_SCENARIO_ID_KEY, urlScenarioId);
        }
        isScenarioLoaded = true;
        console.log('✅ [INIT] Scenario ID from URL saved to storage (overriding old values)');
    } else {
        // URL에 없으면 sessionStorage에서 복원
        if (!currentScenarioId) {
            currentScenarioId = sessionStorage.getItem(CURRENT_SCENARIO_ID_KEY);
            if (currentScenarioId) {
                console.log('📋 [INIT] Scenario ID restored from storage:', currentScenarioId);
                isScenarioLoaded = true;
            }
        }
    }

    // ✅ [DEBUG] 데이터 정합성 검사 - 타입 확인 추가
    console.log('🛠️ [DEBUG] Loading Scenario:', currentScenarioId, '(type:', typeof currentScenarioId, ')');
    console.log('🛠️ [DEBUG] with Session:', currentSessionId, '(type:', typeof currentSessionId, ')');
    console.log('🔍 [SYNC CHECK] Data consistency verification:');
    console.log('  - URL parameter (session_id):', urlSessionId || 'N/A');
    console.log('  - URL parameter (scenario_id):', urlScenarioId || 'N/A');
    console.log('  - Storage (trpg_session_key):', sessionStorage.getItem('trpg_session_key'));
    console.log('  - Storage (trpg_scenario_id):', sessionStorage.getItem('trpg_scenario_id'));
    console.log('  - Current active Session ID:', currentSessionId);
    console.log('  - Current active Scenario ID:', currentScenarioId);

    // ✅ [수정 3] 데이터 정합성 확인 - API 호출 전 최신 ID 확인
    if (currentSessionId) {
        // UI에 세션 ID 즉시 표시
        const sessionIdDisplay = document.getElementById('session-id-display');
        if (sessionIdDisplay) {
            sessionIdDisplay.textContent = currentSessionId;
            sessionIdDisplay.classList.remove('text-gray-300');
            sessionIdDisplay.classList.add('text-green-400');
        }

        // ✅ API 호출 직전 최종 검사
        console.log('🚀 [SYNC CHECK] Final Session ID before API call:', currentSessionId);

        // ✅ [FIX 4] 디버그 모드가 켜져있으면 서버에서 최신 상태 조회
        const isDebugActive = localStorage.getItem(DEBUG_MODE_KEY) === 'true';
        if (isDebugActive) {
            console.log('🔍 [INIT] Debug mode active, fetching latest state from server...');
            console.log('🔍 [INIT] Will call API with session ID:', currentSessionId);
            fetchLatestSessionState();
        } else {
            // 디버그 모드가 꺼져있어도 기존 DB fetch 유지 (하위 호환성)
            console.log('🔍 [INIT] Fetching game data from DB with session ID:', currentSessionId);
            window.fetchGameDataFromDB();
        }
    } else {
        // ✅ [작업 2-3] 세션을 찾지 못했을 때 구체적인 안내
        console.warn('⚠️ [INIT] No session found. Please load a scenario from the main page.');
    }

    // 모델 버전 초기화 (가장 먼저 실행)
    const providerSelect = document.getElementById('provider-select');
    const modelVersionSelect = document.getElementById('model-version-select');

    if (providerSelect && modelVersionSelect) {
        // 이전에 저장된 제공사 복원
        const savedProvider = sessionStorage.getItem(MODEL_PROVIDER_KEY);
        if (savedProvider) {
            providerSelect.value = savedProvider;
        }

        // 모델 버전 옵션 초기화
        updateModelVersions();

        // 제공사 변경 시 처리
        providerSelect.addEventListener('change', function() {
            updateModelVersions();
            console.log('🤖 제공사 변경됨:', this.value);
        });

        // 모델 버전 변경 시 저장
        modelVersionSelect.addEventListener('change', function() {
            sessionStorage.setItem(MODEL_VERSION_KEY, this.value);
            console.log('🤖 모델 저장됨:', this.value);
        });
    } else {
        console.error('❌ 모델 선택 요소를 찾을 수 없습니다:', { providerSelect, modelVersionSelect });
    }

    // 채팅 로그 복원
    restoreChatLog();

    // 디버그 모드 상태 복원 (localStorage로 변경)
    const savedDebugMode = localStorage.getItem(DEBUG_MODE_KEY);
    const debugIcon = document.getElementById('debug-icon');
    if (savedDebugMode === 'true') {
        const debugInfoArea = document.getElementById('debug-info-area');
        if (debugInfoArea) {
            debugInfoArea.classList.remove('hidden');
            if (debugIcon) {
                debugIcon.classList.remove('text-gray-500');
                debugIcon.classList.add('text-indigo-400');
            }

            // ✅ FIX: 세션 ID가 있을 때만 DB에서 데이터 불러오기
            if (currentSessionId) {
                fetchGameDataFromDB();
            } else {
                showEmptyDebugState();
            }
        }
        lucide.createIcons();
    }

    const form = document.getElementById('game-form');
    if (form) {
        form.addEventListener('submit', function(e) {
            e.preventDefault();
            if (isGameEnded) return;
            const input = form.querySelector('input[name="action"]');
            if (input.value.trim()) submitWithStreaming(input.value.trim());
        });
    }

    // 아이콘 재생성 (모든 초기화 후)
    setTimeout(() => {
        lucide.createIcons();
    }, 100);
});

document.body.addEventListener('htmx:afterSwap', function(evt) {
    if (evt.detail.target.id === 'init-result') {
        closeLoadModal();
        clearChatLog();
        isGameEnded = false;
        enableGameUI();
        const chatLog = document.getElementById('chat-log');
        Array.from(chatLog.children).forEach(child => {
            if (child.id !== 'init-result' && child.id !== 'ai-loading') child.remove();
        });
    }
});

lucide.createIcons();

// 사이드바 로직 제거됨 (작업 2)
