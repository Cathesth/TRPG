// debug_module.js - 디버그 사이드바 제어

// ✅ [FIX 5] 빈 상태를 표시하는 함수 추가
function showEmptyDebugState() {
    const worldStateArea = document.getElementById('world-state-area');
    const npcStatusArea = document.getElementById('npc-status-area');

    if (worldStateArea) {
        worldStateArea.innerHTML = `
            <div class="text-gray-500 text-xs text-center py-2 bg-gray-800/50 rounded border border-gray-700 border-dashed">
                World State 데이터 없음
            </div>
        `;
    }

    if (npcStatusArea) {
        npcStatusArea.innerHTML = `
            <div class="text-gray-500 text-xs text-center py-2 bg-gray-800/50 rounded border border-gray-700 border-dashed">
                NPC 데이터 없음
            </div>
        `;
    }

    console.log('ℹ️ [Debug] Empty state displayed');
}

// 디버그 정보 토글 함수
function toggleDebugInfo() {
    const debugInfoArea = document.getElementById('debug-info-area');
    const debugIcon = document.getElementById('debug-icon');

    // 현재 상태 확인
    const isDebugActive = localStorage.getItem(DEBUG_MODE_KEY) === 'true';

    if (isDebugActive) {
        // ✅ [FIX 5] 디버그 모드 끄기 - UI만 숨기고 sessionStorage는 절대 지우지 않음
        debugInfoArea.classList.add('hidden');
        localStorage.setItem(DEBUG_MODE_KEY, 'false');
        if (debugIcon) {
            debugIcon.classList.remove('text-indigo-400');
            debugIcon.classList.add('text-gray-500');
        }
        console.log('🔍 [Debug Toggle OFF] UI hidden, sessionStorage preserved');
    } else {
        // ✅ [FIX 5] 디버그 모드 켜기 - 서버 최신 데이터 조회
        debugInfoArea.classList.remove('hidden');
        localStorage.setItem(DEBUG_MODE_KEY, 'true');
        if (debugIcon) {
            debugIcon.classList.remove('text-gray-500');
            debugIcon.classList.add('text-indigo-400');
        }

        // ✅ [FIX 5] 세션 ID 복원 후 서버 최신 상태 조회
        if (!currentSessionId) {
            currentSessionId = sessionStorage.getItem(CURRENT_SESSION_ID_KEY) || sessionStorage.getItem('trpg_session_key');
        }

        if (currentSessionId) {
            console.log('🔍 [Debug Toggle ON] Fetching latest state from server...');
            fetchLatestSessionState();
        } else {
            console.log('⚠️ [Debug Toggle ON] No session ID, showing empty state');
            showEmptyDebugState();
        }
    }

    lucide.createIcons();
}

// ✅ [FIX 1] 서버에서 최신 세션 상태를 조회하는 함수
async function fetchLatestSessionState() {
    if (!currentSessionId) {
        console.warn('⚠️ [FETCH] No session ID available');
        showEmptyDebugState();
        return;
    }

    try {
        console.log(`📡 [FETCH] Requesting session state: ${currentSessionId}`);
        const response = await fetch(`/game/session_state?session_id=${currentSessionId}`);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();

        if (data.success) {
            console.log('✅ [FETCH] Session state received from server:', data);

            // ✅ [FIX 1] 세션 ID와 시나리오 ID 갱신
            if (data.session_id) {
                currentSessionId = data.session_id;
                sessionStorage.setItem(CURRENT_SESSION_ID_KEY, data.session_id);
                sessionStorage.setItem('trpg_session_key', data.session_id);
            }

            if (data.scenario_id) {
                currentScenarioId = data.scenario_id;
                sessionStorage.setItem(CURRENT_SCENARIO_ID_KEY, data.scenario_id);
            }

            // ✅ [FIX 1] UI 업데이트 (서버 최신 데이터 기준)
            if (data.world_state) {
                // ✅ turn_count가 world_state에 있으면 최상위로 복사
                if (data.world_state.turn_count !== undefined) {
                    data.world_state.turn_count = data.world_state.turn_count;
                } else if (data.turn_count !== undefined) {
                    data.world_state.turn_count = data.turn_count;
                }
                updateWorldState(data.world_state);
            }

            if (data.player_state && data.player_state.player_vars) {
                updateStats(data.player_state.player_vars);
            }

            // NPC 상태는 world_state에서 추출
            if (data.world_state && data.world_state.npcs) {
                updateNPCStatus({ npcs: data.world_state.npcs });
            }

            lucide.createIcons();
        } else {
            console.error('❌ [FETCH] Failed to fetch session state:', data.error);
            showEmptyDebugState();
        }
    } catch (err) {
        console.error('❌ [FETCH] Error:', err);
        showEmptyDebugState();
    }
}

// 디버그 모드에서 전체 씬 보기 함수
function openDebugScenesView() {
    // ✅ [FIX 3] 시나리오 ID와 세션 ID를 모두 확인
    if (!currentScenarioId) {
        currentScenarioId = sessionStorage.getItem(CURRENT_SCENARIO_ID_KEY);
    }

    if (!currentSessionId) {
        currentSessionId = sessionStorage.getItem(CURRENT_SESSION_ID_KEY) || sessionStorage.getItem('trpg_session_key');
    }

    if (!currentScenarioId) {
        showToast('시나리오를 먼저 불러와주세요.', 'error');
        return;
    }

    // ✅ [FIX 2&4] 세션 ID와 시나리오 ID를 확실히 저장
    if (currentSessionId) {
        sessionStorage.setItem(CURRENT_SESSION_ID_KEY, currentSessionId);
        sessionStorage.setItem('trpg_session_key', currentSessionId);
        console.log('💾 [Navigation] Saved session ID:', currentSessionId);
    }

    if (currentScenarioId) {
        sessionStorage.setItem(CURRENT_SCENARIO_ID_KEY, currentScenarioId);
        console.log('💾 [Navigation] Saved scenario ID:', currentScenarioId);
    }

    // ✅ [FIX 2] 내부 네비게이션 플래그 설정
    isInternalNavigation = true;
    sessionStorage.setItem(NAVIGATION_FLAG_KEY, 'true');

    // 시나리오 ID를 쿼리 파라미터로 전달
    window.location.href = `/views/debug_scenes?scenario_id=${currentScenarioId}`;
}

// NPC 상태 업데이트 함수
function updateNPCStatus(npcData) {
    const npcStatusArea = document.getElementById('npc-status-area');
    if (!npcStatusArea) return;

    // npcData가 직접 NPC 딕셔너리인 경우와 statsData에서 추출한 경우 모두 처리
    let npcs = {};

    if (npcData.world_state && npcData.world_state.npcs) {
        // statsData에서 추출한 경우
        npcs = npcData.world_state.npcs;
    } else if (npcData.npcs) {
        // world_state에서 추출한 경우
        npcs = npcData.npcs;
    } else {
        // 직접 NPC 딕셔너리인 경우
        npcs = npcData;
    }

    // player_vars의 키들을 필터링 (NPC가 아닌 것들 제거)
    const invalidKeys = ['hp', 'max_hp', 'mp', 'max_mp', 'gold', 'sanity', 'radiation', 'inventory', 'quests', 'flags', 'custom_stats'];
    const filteredNpcs = {};

    for (const [npcName, npcData] of Object.entries(npcs)) {
        // 키가 invalidKeys에 없고, 값이 객체(NPC 데이터)인 경우만 추가
        if (!invalidKeys.includes(npcName) &&
            typeof npcData === 'object' &&
            npcData !== null &&
            !Array.isArray(npcData)) {

            // NPC 데이터인지 확인 (status, emotion, relationship 등의 속성이 있어야 함)
            if (npcData.hasOwnProperty('status') ||
                npcData.hasOwnProperty('emotion') ||
                npcData.hasOwnProperty('relationship') ||
                npcData.hasOwnProperty('name')) {
                filteredNpcs[npcName] = npcData;
            }
        }
    }

    if (!filteredNpcs || Object.keys(filteredNpcs).length === 0) {
        npcStatusArea.innerHTML = `
            <div class="text-gray-500 text-xs text-center py-2 bg-gray-800/50 rounded border border-gray-700 border-dashed">
                NPC 데이터 없음
            </div>
        `;
        return;
    }

    let html = '';
    for (const [npcName, npcData] of Object.entries(filteredNpcs)) {
        const status = npcData.status || 'alive';
        const hp = npcData.hp !== undefined ? npcData.hp : '?';
        const maxHp = npcData.max_hp || 100;
        const relationship = npcData.relationship !== undefined ? npcData.relationship : 50;
        const emotion = npcData.emotion || 'neutral';
        const location = npcData.location || '?';
        const isHostile = npcData.is_hostile || false;

        // 상태에 따른 색상
        const statusColor = status === 'alive' ? 'text-green-400' :
                          status === 'dead' ? 'text-red-400' : 'text-yellow-400';

        // 관계도에 따른 색상
        const relationColor = relationship >= 70 ? 'text-green-400' :
                            relationship >= 40 ? 'text-blue-400' :
                            relationship >= 20 ? 'text-yellow-400' : 'text-red-400';

        html += `
            <div class="bg-gray-800/50 rounded p-2 border border-gray-700 text-xs">
                <div class="flex items-center justify-between mb-1">
                    <span class="font-bold text-white flex items-center gap-1">
                        <i data-lucide="${isHostile ? 'skull' : 'user'}" class="w-3 h-3 ${isHostile ? 'text-red-500' : 'text-blue-400'}"></i>
                        ${npcName}
                    </span>
                    <span class="${statusColor} text-[10px] font-bold">${status.toUpperCase()}</span>
                </div>
                <div class="space-y-0.5 text-[10px] text-gray-400">
                    <div class="flex justify-between">
                        <span>HP:</span>
                        <span class="text-white">${hp}/${maxHp}</span>
                    </div>
                    <div class="flex justify-between">
                        <span>관계도:</span>
                        <span class="${relationColor}">${relationship}</span>
                    </div>
                    <div class="flex justify-between">
                        <span>감정:</span>
                        <span class="text-white">${emotion}</span>
                    </div>
                    <div class="flex justify-between">
                        <span>위치:</span>
                        <span class="text-white">${location}</span>
                    </div>
                </div>
            </div>
        `;
    }

    npcStatusArea.innerHTML = html;
    lucide.createIcons();
}

// World State 업데이트 함수
function updateWorldState(worldStateData) {
    const worldStateArea = document.getElementById('world-state-area');
    if (!worldStateArea) return;

    // worldStateData가 직접 world_state인 경우와 statsData에서 추출한 경우 모두 처리
    let worldState = {};

    if (worldStateData.world_state) {
        // statsData에서 추출한 경우
        worldState = worldStateData.world_state;
    } else {
        // 직접 world_state인 경우
        worldState = worldStateData;
    }

    if (!worldState || Object.keys(worldState).length === 0) {
        worldStateArea.innerHTML = `
            <div class="text-gray-500 text-xs text-center py-2 bg-gray-800/50 rounded border border-gray-700 border-dashed">
                World State 데이터 없음
            </div>
        `;
        return;
    }

    const time = worldState.time || {};
    const day = time.day || 1;
    const phase = time.phase || 'morning';
    const turnCount = worldState.turn_count || 0;
    const stuckCount = worldState.stuck_count || 0;
    const globalFlags = worldState.global_flags || {};

    // ✅ [FIX 1] 위치 정보 처리 강화 - 실제 데이터가 있을 때만 표시
    let locationDisplay = null;

    // worldState.location을 최우선으로 사용 (백엔드에서 동기화된 데이터)
    const sceneId = worldState.location || worldState.current_scene_id;
    const sceneTitle = worldState.current_scene_title;

    // 실제 데이터가 존재하는지 엄격히 체크
    if (sceneId && sceneId !== '?' && sceneId !== 'Unknown' && sceneId !== '') {
        if (sceneTitle && sceneTitle !== '?' && sceneTitle !== 'Unknown' && sceneTitle !== '') {
            // Scene ID와 제목 모두 유효한 경우
            locationDisplay = `${sceneId} (${sceneTitle})`;
        } else {
            // ID만 유효한 경우
            locationDisplay = sceneId;
        }
    } else if (sceneTitle && sceneTitle !== '?' && sceneTitle !== 'Unknown' && sceneTitle !== '') {
        // 제목만 유효한 경우
        locationDisplay = sceneTitle;
    }

    // 데이터가 없으면 '위치 정보 없음'으로 표시
    if (!locationDisplay) {
        locationDisplay = '<span class="text-gray-500">위치 정보 없음</span>';
    }

    // 시간대에 따른 아이콘
    const phaseIcon = phase === 'morning' ? 'sunrise' :
                     phase === 'afternoon' ? 'sun' : 'moon';

    // 시간대 한글 변환
    const phaseText = phase === 'morning' ? '아침' :
                     phase === 'afternoon' ? '오후' : '밤';

    // stuck_count에 따른 레벨 텍스트 및 색상
    let stuckLevelText = '초기 시도';
    let stuckBarColor = 'bg-green-500';
    let stuckTextColor = 'text-green-400';
    let stuckBarWidth = Math.min((stuckCount / 6) * 100, 100);

    if (stuckCount >= 4) {
        stuckLevelText = '장기 정체 (강한 힌트)';
        stuckBarColor = 'bg-red-500';
        stuckTextColor = 'text-red-400';
    } else if (stuckCount >= 2) {
        stuckLevelText = '반복 실패 (중간 힌트)';
        stuckBarColor = 'bg-yellow-500';
        stuckTextColor = 'text-yellow-400';
    } else if (stuckCount >= 1) {
        stuckLevelText = '초기 시도 (약한 힌트)';
        stuckBarColor = 'bg-green-500';
        stuckTextColor = 'text-green-400';
    }

    let html = `
        <div class="bg-gray-800/50 rounded p-2 border border-gray-700 space-y-1.5">
            <div class="flex justify-between items-center">
                <span class="text-gray-400">시간:</span>
                <span class="text-white flex items-center gap-1">
                    <i data-lucide="${phaseIcon}" class="w-3 h-3 text-yellow-400"></i>
                    ${day}일차, ${phaseText}
                </span>
            </div>
            <div class="flex justify-between items-center">
                <span class="text-gray-400">위치:</span>
                <span class="text-white text-xs">${locationDisplay}</span>
            </div>
            <div class="flex justify-between items-center">
                <span class="text-gray-400">턴 수:</span>
                <span class="text-white">${turnCount}</span>
            </div>
            <div class="border-t border-gray-700 pt-1.5 mt-1.5">
                <div class="flex justify-between items-center mb-1">
                    <span class="text-gray-400 text-xs">정체 카운트:</span>
                    <span class="${stuckTextColor} font-bold">${stuckCount}</span>
                </div>
                <div class="w-full bg-gray-700 rounded-full h-1.5 overflow-hidden mb-1">
                    <div class="${stuckBarColor} h-full transition-all duration-300" style="width: ${stuckBarWidth}%"></div>
                </div>
                <div class="text-[10px] text-gray-500">
                    힌트 강도: <span class="${stuckTextColor}">${stuckLevelText}</span>
                </div>
            </div>
    `;

    // 전역 플래그가 있으면 표시
    if (Object.keys(globalFlags).length > 0) {
        html += `
            <div class="border-t border-gray-700 pt-1.5 mt-1.5">
                <div class="text-gray-500 text-[10px] mb-1">전역 플래그:</div>
                <div class="space-y-0.5">
        `;
        for (const [flag, value] of Object.entries(globalFlags)) {
            const icon = value ? 'check-circle' : 'x-circle';
            const color = value ? 'text-green-400' : 'text-gray-500';
            html += `
                <div class="flex items-center gap-1 text-[10px]">
                    <i data-lucide="${icon}" class="w-2.5 h-2.5 ${color}"></i>
                    <span class="text-gray-400">${flag}:</span>
                    <span class="${color}">${value}</span>
                </div>
            `;
        }
        html += `
                </div>
            </div>
        `;
    }

    html += `</div>`;

    worldStateArea.innerHTML = html;
    lucide.createIcons();
}

// 외부에서 접근 가능하도록 window 객체에 할당
window.toggleDebugInfo = toggleDebugInfo;
window.openDebugScenesView = openDebugScenesView;
window.updateNPCStatus = updateNPCStatus;
window.updateWorldState = updateWorldState;
window.fetchLatestSessionState = fetchLatestSessionState;
window.showEmptyDebugState = showEmptyDebugState;
