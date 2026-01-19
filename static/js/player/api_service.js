// api_service.js - 서버 통신 함수

// 시나리오 플레이 함수
async function playScenario(filename, btn) {
    // 이미 게임이 진행 중이라면 경고
    const savedLog = sessionStorage.getItem(CHAT_LOG_KEY);
    if (savedLog && savedLog.length > 0) {
        if (!confirm('현재 진행 중인 시나리오가 있습니다.\n새 시나리오를 불러오면 진행 내역이 초기화됩니다.\n계속하시겠습니까?')) {
            return;
        }
    }

    const originalText = btn.innerHTML;
    btn.innerHTML = '<i data-lucide="loader-2" class="w-4 h-4 animate-spin"></i> 로딩...';
    btn.disabled = true;
    lucide.createIcons();

    try {
        const formData = new FormData();
        formData.append('filename', filename);

        const res = await fetch('/api/load_scenario', {
            method: 'POST',
            body: formData
        });

        if (res.ok) {
            const data = await res.json();

            // 서버에서 받은 새로운 세션 키를 sessionStorage에 즉시 저장
            if (data.session_key) {
                currentSessionId = data.session_key;
                sessionStorage.setItem('current_session_id', data.session_key);
                sessionStorage.setItem('trpg_session_key', data.session_key);
                console.log('🆕 New session created:', data.session_key);
            }

            // 시나리오 ID도 함께 저장
            if (data.scenario_id) {
                currentScenarioId = data.scenario_id;
                sessionStorage.setItem(CURRENT_SCENARIO_ID_KEY, data.scenario_id);
                console.log('📋 Scenario ID saved:', data.scenario_id);
            }

            // 기존 게임 상태 완전히 초기화
            sessionStorage.removeItem(CHAT_LOG_KEY);
            sessionStorage.removeItem(GAME_ENDED_KEY);
            sessionStorage.removeItem('trpg_world_state');
            sessionStorage.removeItem('trpg_player_stats');
            console.log('🧹 Previous game state cleared');

            // 새 시나리오 로드 상태 설정
            sessionStorage.setItem(SCENARIO_LOADED_KEY, 'true');
            sessionStorage.setItem(CURRENT_SCENARIO_KEY, filename);

            // 내부 네비게이션으로 설정
            isInternalNavigation = true;

            closeLoadModal();

            // UI 초기화
            resetGameUI();
            showToast(data.message || '시나리오가 로드되었습니다!', 'success');

            // ✅ [FIX 4] 디버그 모드가 활성화되어 있다면 즉시 서버 최신 상태 동기화
            const isDebugActive = localStorage.getItem(DEBUG_MODE_KEY) === 'true';
            if (isDebugActive && currentSessionId) {
                console.log('🔍 [Load] Debug mode active, fetching latest session state...');
                // 약간의 지연 후 실행 (UI 초기화 완료 대기)
                setTimeout(() => {
                    if (typeof fetchLatestSessionState === 'function') {
                        fetchLatestSessionState();
                    }
                }, 300);
            }
        } else {
            const text = await res.text();
            showToast('로드 실패: ' + text, 'error');
            btn.innerHTML = originalText;
            btn.disabled = false;
        }
    } catch (e) {
        console.error(e);
        showToast('오류가 발생했습니다.', 'error');
        btn.innerHTML = originalText;
        btn.disabled = false;
    }
}

// 시나리오 삭제 함수
async function deleteScenario(filename, title, btnElement) {
    if (!confirm(`"${title}" 시나리오를 삭제하시겠습니까?\n이 작업은 되돌릴 수 없습니다.`)) {
        return;
    }

    try {
        const response = await fetch('/api/delete_scenario', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: filename })
        });

        const result = await response.json();

        if (result.success) {
            const card = btnElement.closest('.bg-gray-800, .bg-rpg-800');
            if (card) {
                card.style.transition = 'opacity 0.3s, transform 0.3s';
                card.style.opacity = '0';
                card.style.transform = 'scale(0.9)';
                setTimeout(() => {
                    card.remove();
                    const container = document.getElementById('scenario-list-container');
                    if (container && container.children.length === 0) {
                        container.innerHTML = '<div class="col-span-1 md:col-span-2 text-center text-gray-500 py-8">저장된 시나리오가 없습니다.</div>';
                    }
                }, 300);
            }
            showToast('시나리오가 삭제되었습니다.', 'success');
        } else {
            showToast('삭제 실패: ' + (result.error || '알 수 없는 오류'), 'error');
        }
    } catch (error) {
        console.error('Delete error:', error);
        showToast('삭제 중 오류가 발생했습니다.', 'error');
    }
}

// 시나리오 공개/비공개 토글 함수
async function publishScenario(filename, btnElement) {
    const card = btnElement.closest('.bg-gray-800');
    const statusBadge = card.querySelector('span[class*="bg-green-900"], span[class*="bg-gray-700"]');
    const currentStatus = statusBadge.textContent.trim();
    const isCurrentlyPublic = currentStatus === 'PUBLIC';

    try {
        const response = await fetch('/api/publish_scenario', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: filename })
        });

        const result = await response.json();

        if (result.success) {
            // UI 즉시 업데이트
            if (isCurrentlyPublic) {
                statusBadge.className = 'ml-2 text-[10px] bg-gray-700 text-gray-300 px-1 rounded';
                statusBadge.textContent = 'PRIVATE';
            } else {
                statusBadge.className = 'ml-2 text-[10px] bg-green-900 text-green-300 px-1 rounded';
                statusBadge.textContent = 'PUBLIC';
            }

            showToast(result.message || '상태가 변경되었습니다.', 'success');
        } else {
            showToast('변경 실패: ' + (result.error || '알 수 없는 오류'), 'error');
        }
    } catch (error) {
        console.error('Publish error:', error);
        showToast('처리 중 오류가 발생했습니다.', 'error');
    }
}

// Railway DB에서 게임 데이터 불러오기
async function fetchGameDataFromDB() {
    // ✅ [작업 4] currentSessionId가 비어있으면 sessionStorage에서 복원 (메모리 유실 대비)
    if (!currentSessionId) {
        currentSessionId = sessionStorage.getItem("current_session_id") || sessionStorage.getItem("trpg_session_key");
        if (currentSessionId) {
            console.log('🔄 [FETCH] Restored session ID from sessionStorage:', currentSessionId);
        } else {
            console.warn('⚠️ [FETCH] No session ID available in memory or storage');
            showEmptyDebugState();
            return;
        }
    }

    const sessionKey = currentSessionId;

    if (!sessionKey) {
        console.warn('⚠️ No session key available');
        showEmptyDebugState();
        return;
    }

    try {
        console.log(`📡 Fetching session data: ${sessionKey}`);
        const response = await fetch(`/game/session/${sessionKey}`);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();

        if (data.success) {
            console.log('✅ Data fetched from Railway DB:', data);

            // ✅ [작업 4] 프론트엔드 데이터 매핑 강제화 - player_state.current_scene_id가 절대 진리
            if (data.player_state && data.world_state) {
                // 서버에서 받은 world_state.location 무시하고 player_state.current_scene_id로 강제 덮어쓰기
                data.world_state.location = data.player_state.current_scene_id;
                data.world_state.current_scene_id = data.player_state.current_scene_id;
                console.log('🔄 [SYNC] Forced location sync: world_state.location =', data.world_state.location);
            }

            // ✅ [작업 4] current_scene_id가 없으면 DB의 current_scene_id 필드에서 가져오기
            if (data.current_scene_id && data.world_state) {
                data.world_state.location = data.current_scene_id;
                data.world_state.current_scene_id = data.current_scene_id;
                console.log('🔄 [SYNC] Applied fallback location from current_scene_id:', data.current_scene_id);
            }

            // 2단계: 세션 ID 갱신 및 화면 즉시 반영
            if (data.player_state && data.player_state.session_id) {
                currentSessionId = data.player_state.session_id;
                sessionStorage.setItem('current_session_id', currentSessionId);
                sessionStorage.setItem('trpg_session_key', currentSessionId);

                const sessionIdDisplay = document.getElementById('session-id-display');
                if (sessionIdDisplay) {
                    sessionIdDisplay.textContent = currentSessionId;
                    sessionIdDisplay.classList.remove('text-gray-300');
                    sessionIdDisplay.classList.add('text-green-400');
                }
                console.log('🔄 [SESSION] Updated session ID from server:', currentSessionId);
            }

            // 3단계: UI 업데이트 (순서 중요: World State -> Player Stats -> NPC Status)
            // ✅ [작업 4] World State 덮어쓰기 - 강제 동기화 후 updateWorldState 호출
            if (data.world_state) {
                updateWorldState(data.world_state);
                console.log('🌍 [WORLD_STATE] Updated from DB:', data.world_state);
            }

            // Player Stats 덮어쓰기
            if (data.player_state && data.player_state.player_vars) {
                updateStats(data.player_state.player_vars);
                console.log('📊 [PLAYER_VARS] Updated from DB:', data.player_state.player_vars);
            }

            // NPC Status 업데이트
            if (data.npc_status) {
                updateNPCStatus(data.npc_status);
                console.log('🤖 [NPC_STATUS] Updated from DB:', data.npc_status);
            }

            lucide.createIcons();
            console.log('✅ All data updated from Railway DB - Client state overwritten');
        } else {
            console.error('❌ Failed to fetch from DB:', data.error);
            showEmptyDebugState();
        }
    } catch (err) {
        console.error('❌ DB fetch error:', err);
        showEmptyDebugState();
    }
}

// SSE 스트리밍으로 게임 액션 제출
async function submitWithStreaming(actionText) {
    if (isGameEnded) return;

    // 게임이 시작되었음을 표시
    hasGameStarted = true;
    isStreaming = true;

    const form = document.getElementById('game-form');
    const chatLog = document.getElementById('chat-log');
    const loadingIndicator = document.getElementById('ai-loading');
    const input = form.querySelector('input[name="action"]');
    const scenesBtn = document.getElementById('scenes-btn');
    const providerSelect = document.getElementById('provider-select');
    const modelVersionSelect = document.getElementById('model-version-select');

    const introMsg = document.getElementById('intro-message');
    if (introMsg) introMsg.remove();

    form.classList.add('streaming');
    input.readOnly = true;
    chatLog.classList.add('processing');

    // 스트리밍 중 전체 씬 보기 버튼 비활성화
    if (scenesBtn) {
        scenesBtn.disabled = true;
        scenesBtn.title = "답변 생성 중에는 사용할 수 없습니다";
    }

    // 플레이어 메시지 추가
    const playerMsgHtml = `
    <div class="flex gap-4 fade-in mb-4 justify-end">
        <div class="flex-1 text-right">
            <div class="text-gray-400 text-xs font-bold mb-1">Player</div>
            <div class="bg-[#2a2a30] border-gray-600 p-3 rounded-lg border text-white text-sm inline-block text-left">
                ${actionText}
            </div>
        </div>
        <div class="w-8 h-8 rounded-lg bg-gray-700 flex items-center justify-center shrink-0">
            <i data-lucide="user" class="text-white w-4 h-4"></i>
        </div>
    </div>`;
    loadingIndicator.insertAdjacentHTML('beforebegin', playerMsgHtml);
    input.value = '';

    // GM 응답 컨테이너
    const gmContainer = document.createElement('div');
    gmContainer.className = 'flex gap-4 fade-in mb-4';
    gmContainer.innerHTML = `
        <div class="w-8 h-8 rounded-lg bg-indigo-900 flex items-center justify-center shrink-0">
            <i data-lucide="bot" class="text-white w-4 h-4"></i>
        </div>
        <div class="flex-1">
            <div class="text-indigo-400 text-xs font-bold mb-1">GM</div>
            <div id="gm-streaming-content" class="bg-[#1a1a1e] border-gray-700 p-3 rounded-lg border text-gray-300 text-sm leading-relaxed serif-font">
                <span class="streaming-cursor">▌</span>
            </div>
        </div>`;
    loadingIndicator.insertAdjacentHTML('beforebegin', gmContainer.outerHTML);

    const contentDiv = document.getElementById('gm-streaming-content');
    loadingIndicator.classList.remove('hidden');
    loadingIndicator.classList.add('flex');
    lucide.createIcons();
    scrollToBottom();

    try {
        // JSON 방식으로 전송
        const requestBody = {
            action: actionText
        };

        // 선택한 모델 추가
        if (providerSelect && modelVersionSelect) {
            requestBody.model = modelVersionSelect.value;
            requestBody.provider = providerSelect.value;
        }

        // 세션 ID 포함
        if (currentSessionId) {
            requestBody.session_id = currentSessionId;
            console.log('📤 Sending session_id to server:', currentSessionId);
        } else {
            console.warn('⚠️ No session_id available, server will create new session');
        }

        // 현재 시나리오 ID 전송
        if (currentScenarioId) {
            requestBody.scenario_id = currentScenarioId;
            console.log('📤 Sending scenario_id to server:', currentScenarioId);
        }

        const response = await fetch('/game/act_stream', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(requestBody)
        });

        // 서버 에러 체크
        if (!response.ok) {
            let errorMsg = '서버 오류';
            try {
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let errorText = '';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    errorText += decoder.decode(value, { stream: true });
                }

                try {
                    const errData = JSON.parse(errorText);
                    errorMsg = errData.error || errData.detail || errorText;
                } catch {
                    errorMsg = errorText || `HTTP ${response.status}`;
                }
            } catch (e) {
                errorMsg = `HTTP ${response.status}: ${e.message}`;
            }
            throw new Error(errorMsg);
        }

        // SSE 스트리밍 처리
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let narratorText = '';
        let buffer = '';
        let currentContent = '';

        // 응답 시간 측정 타이머 시작
        responseStartTime = Date.now();
        responseTimerInterval = setInterval(() => {
            const elapsedTime = Date.now() - responseStartTime;
            const seconds = Math.floor(elapsedTime / 1000);
            const milliseconds = Math.floor((elapsedTime % 1000) / 100);
            const timerText = `⏱ ${seconds}.${milliseconds}초`;
            document.getElementById('response-timer').textContent = timerText;
        }, 100);

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const data = JSON.parse(line.slice(6));
                        // Game Engine의 스트리밍 타입 처리
                        switch (data.type) {
                            case 'prefix':
                                // Check for background image in scene data
                                if (data.background_image) {
                                    currentContent += `<div class="mb-4 relative"><img src="${data.background_image}" alt="Scene Background" class="w-full h-48 object-cover rounded-lg border-2 border-gray-700" /></div>`;
                                }
                                currentContent += data.content;
                                contentDiv.innerHTML = currentContent + '<span id="narrator-stream"></span><span class="streaming-cursor">▌</span>';
                                break;
                            case 'token':
                                narratorText += data.content;
                                const narratorSpan = document.getElementById('narrator-stream');
                                if (narratorSpan) narratorSpan.innerHTML = narratorText;
                                else {
                                    currentContent += data.content;
                                    contentDiv.innerHTML = currentContent + '<span class="streaming-cursor">▌</span>';
                                }
                                scrollToBottom(true);
                                break;
                            case 'section_end':
                                currentContent += narratorText + data.content;
                                narratorText = '';
                                contentDiv.innerHTML = currentContent + '<span id="narrator-stream"></span><span class="streaming-cursor">▌</span>';
                                break;
                            case 'ending_start':
                                currentContent = data.content;
                                contentDiv.innerHTML = currentContent + '<span class="streaming-cursor">▌</span>';
                                narratorText = '';
                                break;
                            case 'retry':
                                const loadingText = document.getElementById('loading-text');
                                if (loadingText) {
                                    loadingText.textContent = `답변을 재생성하고 있습니다... (${data.attempt}/${data.max})`;
                                    loadingText.classList.add('text-yellow-400');
                                }
                                currentContent = '';
                                narratorText = '';
                                contentDiv.innerHTML = '<span class="text-yellow-400 text-xs">🔄 답변 재생성 중...</span><span class="streaming-cursor">▌</span>';
                                break;
                            case 'fallback':
                                currentContent = data.content;
                                contentDiv.innerHTML = currentContent;
                                break;
                            case 'game_ended':
                                disableInput();
                                break;
                            case 'stats':
                                updateStats(data.content);
                                break;
                            case 'world_state':
                                updateWorldState(data.content);
                                break;
                            case 'npc_status':
                                updateNPCStatus(data.content);
                                break;
                            case 'session_key':
                                currentSessionKey = data.content;
                                localStorage.setItem(SESSION_KEY_STORAGE, data.content);
                                console.log('🔑 Session key saved:', data.content);
                                break;
                            case 'session_id':
                                currentSessionId = data.content;
                                // sessionStorage에 세션 ID 저장 (복원용) - 하위 호환성도 유지
                                sessionStorage.setItem('current_session_id', data.content);
                                sessionStorage.setItem('trpg_session_key', data.content);  // 하위 호환성
                                console.log('🆔 Session ID received and updated:', data.content);
                                const sessionIdDisplay = document.getElementById('session-id-display');
                                if (sessionIdDisplay) {
                                    sessionIdDisplay.textContent = currentSessionId;
                                    sessionIdDisplay.classList.remove('text-gray-300');
                                    sessionIdDisplay.classList.add('text-green-400');
                                }
                                break;
                            case 'done':
                                contentDiv.innerHTML = contentDiv.innerHTML.replace('<span class="streaming-cursor">▌</span>', '');

                                // 최종 생성 시간 표시
                                const finalElapsedTime = Date.now() - responseStartTime;
                                const finalSeconds = (finalElapsedTime / 1000).toFixed(1);
                                const timeStamp = `<div class="text-[10px] text-gray-500 mt-2 font-mono">⏱ 생성 시간: ${finalSeconds}초</div>`;
                                contentDiv.innerHTML += timeStamp;

                                // 로딩 메시지 초기화
                                const loadingTextReset = document.getElementById('loading-text');
                                if (loadingTextReset) {
                                    loadingTextReset.textContent = '답변을 생성하고 있습니다...';
                                    loadingTextReset.classList.remove('text-yellow-400');
                                }
                                break;
                            case 'error':
                                contentDiv.innerHTML = `<div class="text-red-500">Error: ${data.content}</div>`;
                                break;
                        }
                    } catch (e) { console.error('Parse error:', e); }
                }
            }
        }
    } catch (error) {
        contentDiv.innerHTML = `<div class="text-red-500">통신 오류: ${error.message}</div>`;
        console.error('Streaming error:', error);
    } finally {
        // 스트리밍 종료
        isStreaming = false;
        clearInterval(responseTimerInterval);

        loadingIndicator.classList.add('hidden');
        loadingIndicator.classList.remove('flex');
        form.classList.remove('streaming');
        if (!isGameEnded) { input.readOnly = false; input.focus(); }
        chatLog.classList.remove('processing');

        // 전체 씬 보기 버튼 다시 활성화
        if (scenesBtn && isScenarioLoaded) {
            scenesBtn.disabled = false;
            scenesBtn.title = "";
        }

        // ✅ [FIX 1] act_stream 완료 후 서버 최신 상태를 조회해서 디버그 패널 갱신
        if (currentSessionId) {
            console.log('🔄 [ACT COMPLETE] Refreshing debug panel from server...');

            // 디버그 모드가 활성화되어 있으면 최신 상태 조회
            const isDebugActive = localStorage.getItem(DEBUG_MODE_KEY) === 'true';
            if (isDebugActive) {
                // ✅ [FIX 1] 약간의 지연 후 조회 (DB 저장 완료 대기)
                setTimeout(() => {
                    fetchLatestSessionState();
                }, 500);
            }
        }

        lucide.createIcons();
        const oldContent = document.getElementById('gm-streaming-content');
        if (oldContent) oldContent.removeAttribute('id');
        const oldNarrator = document.getElementById('narrator-stream');
        if (oldNarrator) oldNarrator.removeAttribute('id');
        scrollToBottom();
        saveChatLog();
    }
}

function submitGameAction(actionText) {
    if (isGameEnded) return;
    submitWithStreaming(actionText);
}

function disableInput() {
    isGameEnded = true;
    const form = document.getElementById('game-form');
    const input = form.querySelector('input[name="action"]');
    const submitBtn = form.querySelector('button[type="submit"]');

    input.disabled = true;
    input.placeholder = "🎮 게임이 종료되었습니다.";
    input.classList.add('opacity-50', 'cursor-not-allowed');
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.classList.add('opacity-50', 'cursor-not-allowed');
    }
}

function saveChatLog() {
    const chatLog = document.getElementById('chat-log');
    let content = '';
    for (const child of chatLog.children) {
        if (child.id !== 'init-result' && child.id !== 'ai-loading') {
            content += child.outerHTML;
        }
    }
    sessionStorage.setItem(CHAT_LOG_KEY, content);
    sessionStorage.setItem(GAME_ENDED_KEY, isGameEnded.toString());
}

function clearChatLog() {
    sessionStorage.removeItem(CHAT_LOG_KEY);
    sessionStorage.removeItem(GAME_ENDED_KEY);
}

// 외부에서 접근 가능하도록 window 객체에 할당
window.playScenario = playScenario;
window.deleteScenario = deleteScenario;
window.publishScenario = publishScenario;
window.fetchGameDataFromDB = fetchGameDataFromDB;
window.submitWithStreaming = submitWithStreaming;
window.submitGameAction = submitGameAction;
window.disableInput = disableInput;
window.saveChatLog = saveChatLog;
window.clearChatLog = clearChatLog;
