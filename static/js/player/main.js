// main.js - DOMContentLoaded 이벤트 및 초기화

document.addEventListener('DOMContentLoaded', function() {
    // 아이콘 초기화
    lucide.createIcons();

    // 새로고침으로 인한 초기화가 필요한 경우 UI 초기화
    const isPageRefresh = performance.navigation.type === 1 ||
                         (performance.getEntriesByType('navigation')[0]?.type === 'reload');
    if (isPageRefresh) {
        initializeEmptyGameUI();
    }

    // ✅ 작업 1: 세션 ID 복원 (sessionStorage에서) - 하위 호환성 포함
    if (!currentSessionId) {
        currentSessionId = sessionStorage.getItem("current_session_id") || sessionStorage.getItem("trpg_session_key");
        if (currentSessionId) {
            console.log('🔄 [INIT] Session ID restored from sessionStorage:', currentSessionId);
            // 세션 ID 표시 업데이트
            const sessionIdDisplay = document.getElementById('session-id-display');
            if (sessionIdDisplay) {
                sessionIdDisplay.textContent = currentSessionId;
                sessionIdDisplay.classList.remove('text-gray-300');
                sessionIdDisplay.classList.add('text-green-400');
            }
        }
    }

    // ✅ 작업 2: 페이지 로드 시 자동 복구 - 세션 ID가 있으면 최신 상태 동기화
    if (currentSessionId) {
        console.log('🔄 [INIT] Auto-recovering game state from DB...');
        fetchGameDataFromDB();
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

            // DB에서 데이터 불러오기
            fetchGameDataFromDB();
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

// 사이드바 로직
(function() {
    const sidebar = document.querySelector('.sidebar');
    const SIDEBAR_STATE_KEY = 'sidebar_expanded';
    let isRestoredState = false;

    if (sessionStorage.getItem(SIDEBAR_STATE_KEY) === 'true') {
        sidebar.style.transition = 'none';
        sidebar.classList.add('expanded');
        isRestoredState = true;
        sessionStorage.removeItem(SIDEBAR_STATE_KEY);
        requestAnimationFrame(() => requestAnimationFrame(() => sidebar.style.transition = ''));
    }

    if (isRestoredState) {
        setTimeout(() => {
            const checkMousePosition = (e) => {
                const rect = sidebar.getBoundingClientRect();
                const isInsideSidebar = e.clientX >= rect.left && e.clientX <= rect.right && e.clientY >= rect.top && e.clientY <= rect.bottom;
                if (!isInsideSidebar) sidebar.classList.remove('expanded');
                document.removeEventListener('mousemove', checkMousePosition);
            };
            document.addEventListener('mousemove', checkMousePosition, { once: true });
        }, 100);
    }

    sidebar.querySelectorAll('a[href], button').forEach(link => {
        link.addEventListener('click', function(e) {
            if (this.tagName === 'A' && this.href) {
                sessionStorage.setItem(SIDEBAR_STATE_KEY, 'true');
                sidebar.classList.add('expanded');
            }
        });
    });
    sidebar.addEventListener('mouseleave', () => sidebar.classList.remove('expanded'));
})();
