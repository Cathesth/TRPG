// ui_manager.js - 화면 렌더링 및 UI 제어

function scrollToBottom(smooth = true) {
    const chatLog = document.getElementById('chat-log');
    if (chatLog) chatLog.scrollTo({ top: chatLog.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

function enableGameUI() {
    isScenarioLoaded = true;
    sessionStorage.setItem(SCENARIO_LOADED_KEY, 'true');
    const form = document.getElementById('game-form');
    const input = form.querySelector('input[name="action"]');
    const submitBtn = form.querySelector('button[type="submit"]');

    if (input) {
        input.disabled = false;
        input.placeholder = "어떤 행동을 하시겠습니까? (예: 문을 연다, 살펴본다...)";
        input.classList.remove('opacity-50', 'cursor-not-allowed');
    }
    if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.classList.remove('opacity-50', 'cursor-not-allowed');
    }
    const scenesBtn = document.getElementById('scenes-btn');
    if (scenesBtn) scenesBtn.disabled = false;
}

function disableGameUI() {
    isScenarioLoaded = false;
    const form = document.getElementById('game-form');
    const input = form.querySelector('input[name="action"]');
    const submitBtn = form.querySelector('button[type="submit"]');

    if (input) {
        input.disabled = true;
        input.placeholder = "시나리오를 먼저 불러와주세요...";
        input.classList.add('opacity-50', 'cursor-not-allowed');
    }
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.classList.add('opacity-50', 'cursor-not-allowed');
    }
    const scenesBtn = document.getElementById('scenes-btn');
    if (scenesBtn) scenesBtn.disabled = true;
}

// UI 초기화 함수 (완전 초기 상태)
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
        showEmptyDebugState();

        // 세션 키 초기화
        currentSessionKey = '';
        localStorage.removeItem(SESSION_KEY_STORAGE);

        // UI 비활성화
        disableGameUI();
    }
}

// 빈 디버그 상태 표시
function showEmptyDebugState() {
    const npcStatusArea = document.getElementById('npc-status-area');
    const worldStateArea = document.getElementById('world-state-area');

    if (npcStatusArea) {
        npcStatusArea.innerHTML = `
            <div class="text-gray-500 text-xs text-center py-2 bg-gray-800/50 rounded border border-gray-700 border-dashed">
                NPC 데이터 없음
            </div>
        `;
    }

    if (worldStateArea) {
        worldStateArea.innerHTML = `
            <div class="text-gray-500 text-xs text-center py-2 bg-gray-800/50 rounded border border-gray-700 border-dashed">
                World State 데이터 없음
            </div>
        `;
    }

    lucide.createIcons();
}

function restoreChatLog() {
    const savedLog = sessionStorage.getItem(CHAT_LOG_KEY);
    const savedGameEnded = sessionStorage.getItem(GAME_ENDED_KEY);
    const savedScenarioLoaded = sessionStorage.getItem(SCENARIO_LOADED_KEY);

    if (savedLog) {
        const chatLog = document.getElementById('chat-log');
        const initResult = document.getElementById('init-result');
        const aiLoading = document.getElementById('ai-loading');

        chatLog.innerHTML = '';
        chatLog.appendChild(initResult);
        initResult.insertAdjacentHTML('afterend', savedLog);
        chatLog.appendChild(aiLoading);

        const intro = document.getElementById('intro-message');
        if (intro) intro.remove();

        lucide.createIcons();
        chatLog.scrollTo({ top: chatLog.scrollHeight, behavior: 'auto' });
    } else if (savedScenarioLoaded === 'true') {
        const intro = document.getElementById('intro-message');
        if (intro) intro.remove();
        const initResult = document.getElementById('init-result');
        initResult.innerHTML = `
        <div class="bg-green-900/30 border border-green-800 text-green-400 p-4 rounded-lg flex items-center gap-3 fade-in mt-4">
            <i data-lucide="check-circle" class="w-6 h-6"></i>
            <div>
                <div class="font-bold">로드 완료!</div>
                <div class="text-sm opacity-80">아래 버튼을 클릭하거나 채팅창에 "시작"을 입력하세요.</div>
            </div>
        </div>
        <button onclick="submitGameAction('시작')"
                class="mt-3 w-full bg-indigo-600 hover:bg-indigo-500 text-white py-3 rounded-lg font-bold flex items-center justify-center gap-2 transition-all hover:scale-[1.02] shadow-lg">
            <i data-lucide="play" class="w-5 h-5"></i>
            게임 시작하기
        </button>
        `;
        lucide.createIcons();
    }

    if (savedGameEnded === 'true') {
        isGameEnded = true;
        disableInput();
    }

    if (savedScenarioLoaded === 'true') enableGameUI();
    else disableGameUI();
}

function resetGameUI() {
    const chatLog = document.getElementById('chat-log');
    const initResult = document.getElementById('init-result');
    const aiLoading = document.getElementById('ai-loading');
    const statsArea = document.getElementById('player-stats-area');

    // 채팅 로그 초기화
    chatLog.innerHTML = '';
    chatLog.appendChild(initResult);

    // 로드 완료 메시지 표시
    initResult.innerHTML = `
    <div class="bg-green-900/30 border border-green-800 text-green-400 p-4 rounded-lg flex items-center gap-3 fade-in mt-4">
        <i data-lucide="check-circle" class="w-6 h-6"></i>
        <div>
            <div class="font-bold">로드 완료!</div>
            <div class="text-sm opacity-80">아래 버튼을 클릭하거나 채팅창에 "시작"을 입력하세요.</div>
        </div>
    </div>
    <button onclick="submitGameAction('시작')"
            class="mt-3 w-full bg-indigo-600 hover:bg-indigo-500 text-white py-3 rounded-lg font-bold flex items-center justify-center gap-2 transition-all hover:scale-[1.02] shadow-lg">
        <i data-lucide="play" class="w-5 h-5"></i>
        게임 시작하기
    </button>
    `;

    chatLog.appendChild(aiLoading);

    // 스탯 영역 초기화
    if (statsArea) {
        statsArea.innerHTML = `
        <div class="text-gray-500 text-sm text-center py-4 bg-gray-800/50 rounded-lg border border-gray-700 border-dashed">
            <i data-lucide="ghost" class="w-6 h-6 mx-auto mb-2 opacity-50"></i>
            데이터 없음<br>
            <span class="text-xs">게임을 시작하면 표시됩니다.</span>
        </div>
        `;
    }

    // 디버그 영역 초기화
    const npcStatusArea = document.getElementById('npc-status-area');
    const worldStateArea = document.getElementById('world-state-area');

    if (npcStatusArea) {
        npcStatusArea.innerHTML = `
            <div class="text-gray-500 text-xs text-center py-2 bg-gray-800/50 rounded border border-gray-700 border-dashed">
                NPC 데이터 없음
            </div>
        `;
    }

    if (worldStateArea) {
        worldStateArea.innerHTML = `
            <div class="text-gray-500 text-xs text-center py-2 bg-gray-800/50 rounded border border-gray-700 border-dashed">
                World State 데이터 없음
            </div>
        `;
    }

    // 상태 초기화
    isGameEnded = false;
    hasGameStarted = false;

    // UI 활성화
    enableGameUI();

    lucide.createIcons();
}

function updateStats(statsData) {
    const statsArea = document.getElementById('player-stats-area');
    if (!statsArea) return;

    // 스탯 표시 로직
    const statConfig = {
        'hp': { icon: 'heart', color: 'red', isBar: true, max: 'max_hp' },
        'mp': { icon: 'zap', color: 'blue', isBar: true, max: 'max_mp' },
        'sanity': { icon: 'brain', color: 'purple', isBar: true, max: 100 },
        'gold': { icon: 'coins', color: 'yellow' }
    };

    let html = `
    <div class="bg-[#1a1e2e] rounded-lg p-4 border border-[#2d2d35] shadow-sm mb-4 fade-in">
        <div class="flex justify-between items-center mb-3">
            <span class="text-xs font-bold text-gray-400 uppercase">Status</span>
            <i data-lucide="activity" class="w-3 h-3 text-red-500"></i>
        </div>
        <div class="space-y-2">`;

    for (const [k, v] of Object.entries(statsData)) {
        // world_state와 내부 플래그 필터링
        if (k !== 'inventory' && k !== 'world_state' && !k.startsWith('max_') && !k.startsWith('npc_appeared_') && !k.startsWith('_')) {
            const config = statConfig[k.toLowerCase()] || { icon: 'circle', color: 'gray' };
            const colorClass = `text-${config.color}-400`;
            const bgColorClass = `bg-${config.color}-500`;

            if (config.isBar) {
                let maxVal = typeof config.max === 'string' ? (statsData[config.max] || 100) : 100;
                let percentage = Math.min(100, Math.max(0, (v / maxVal) * 100));
                html += `
                <div class="mb-2">
                    <div class="flex justify-between items-center mb-1">
                        <span class="text-xs text-gray-400 flex items-center gap-1"><i data-lucide="${config.icon}" class="w-3 h-3 ${colorClass}"></i>${k.toUpperCase()}</span>
                        <span class="text-xs font-bold text-white">${v}/${maxVal}</span>
                    </div>
                    <div class="w-full bg-gray-700 rounded-full h-2 overflow-hidden">
                        <div class="${bgColorClass} h-full transition-all duration-300 rounded-full" style="width: ${percentage}%"></div>
                    </div>
                </div>`;
            } else {
                html += `
                <div class="flex justify-between items-center border-b border-gray-800 py-1.5">
                    <span class="text-xs text-gray-400 flex items-center gap-1"><i data-lucide="${config.icon}" class="w-3 h-3 ${colorClass}"></i>${k.toUpperCase()}</span>
                    <span class="text-white font-bold text-sm">${v}</span>
                </div>`;
            }
        }
    }
    html += '</div>';

    if (statsData.inventory && statsData.inventory.length > 0) {
        html += `
        <div class="border-t border-gray-700 pt-3 mt-3">
            <div class="text-[10px] text-gray-500 mb-2 flex items-center gap-1"><i data-lucide="backpack" class="w-3 h-3"></i>INVENTORY</div>
            <div class="flex flex-wrap gap-1">`;
        for (const item of statsData.inventory) {
            html += `<span class="bg-gray-800 px-2 py-1 rounded text-[10px] text-indigo-300 border border-gray-700 flex items-center gap-1"><i data-lucide="box" class="w-2.5 h-2.5"></i>${item}</span>`;
        }
        html += '</div></div>';
    }
    html += '</div>';

    statsArea.innerHTML = html;
    lucide.createIcons();

    // 디버그 모드가 활성화되어 있으면 디버그 정보도 업데이트
    const isDebugActive = localStorage.getItem(DEBUG_MODE_KEY) === 'true';
    if (isDebugActive) {
        updateDebugInfo(statsData);
    }
}

function updateDebugInfo(statsData) {
    updateNPCStatus(statsData);
    updateWorldState(statsData);
}

function openLoadModal() {
    const modal = document.getElementById('load-modal');
    if (modal) {
        modal.classList.remove('hidden');
        modal.style.display = 'flex';
        const sortSelect = document.getElementById('scenario-sort');
        const sortValue = sortSelect ? sortSelect.value : 'newest';
        htmx.ajax('GET', `/api/scenarios?sort=${sortValue}&filter=all`, {target: '#scenario-list-container', swap: 'innerHTML'});
    }
}

function closeLoadModal() {
    const modal = document.getElementById('load-modal');
    if (modal) {
        modal.classList.add('hidden');
        modal.style.display = 'none';
    }
}

function reloadScenarioList() {
    const sortSelect = document.getElementById('scenario-sort');
    const sortValue = sortSelect ? sortSelect.value : 'newest';
    htmx.ajax('GET', `/api/scenarios?sort=${sortValue}&filter=all`, {target: '#scenario-list-container', swap: 'innerHTML'});
}

function showToast(message, type = 'info') {
    const bgColor = type === 'success' ? 'bg-green-900/90 border-green-500/30 text-green-100' :
                   type === 'error' ? 'bg-red-900/90 border-red-500/30 text-red-100' :
                   'bg-blue-900/90 border-blue-500/30 text-blue-100';

    const icon = type === 'success' ? 'check-circle' :
                type === 'error' ? 'alert-circle' : 'info';

    const toast = document.createElement('div');
    toast.className = `fixed bottom-4 right-4 z-[100] ${bgColor} border px-6 py-4 rounded-xl shadow-2xl backdrop-blur-md flex items-center gap-3`;
    toast.innerHTML = `
        <i data-lucide="${icon}" class="w-5 h-5"></i>
        <span class="font-medium">${message}</span>
    `;

    document.body.appendChild(toast);
    lucide.createIcons();

    setTimeout(() => {
        toast.style.transition = 'opacity 0.3s, transform 0.3s';
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(10px)';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

function editScenario(filename) {
    closeLoadModal();
    isInternalNavigation = true;
    window.location.href = `/views/scenes/edit/${filename}`;
}

function openScenesView() {
    if (isScenarioLoaded) {
        isInternalNavigation = true;
        window.location.href = '/views/scenes';
    }
}

function updateModelVersions() {
    const providerSelect = document.getElementById('provider-select');
    const modelVersionSelect = document.getElementById('model-version-select');

    const provider = providerSelect.value;

    // 기본 옵션 지우기
    modelVersionSelect.innerHTML = '';

    // 제공사에 따른 모델 버전 추가
    let options = [];
    switch (provider) {
        case 'google':
            options = [
                { value: 'openai/google/gemini-2.0-flash-001', label: 'Gemini 2.0 Flash (1M)' },
                { value: 'openai/google/gemini-2.5-flash-lite', label: 'Gemini 2.5 Flash Lite (1M)' },
                { value: 'openai/google/gemini-2.5-flash', label: 'Gemini 2.5 Flash (1M)' },
                { value: 'openai/google/gemini-3-flash-preview', label: 'Gemini 3 Flash Preview (1M)' },
                { value: 'openai/google/gemini-3-pro-preview', label: 'Gemini 3 Pro Preview (1M)' }
            ];
            break;
        case 'anthropic':
            options = [
                { value: 'openai/anthropic/claude-3.5-haiku', label: 'Claude 3.5 Haiku (200K)' },
                { value: 'openai/anthropic/claude-3.5-sonnet', label: 'Claude 3.5 Sonnet (200K)' },
                { value: 'openai/anthropic/claude-sonnet-4', label: 'Claude Sonnet 4 (200K)' },
                { value: 'openai/anthropic/claude-haiku-4.5', label: 'Claude Haiku 4.5 (200K)' },
                { value: 'openai/anthropic/claude-sonnet-4.5', label: 'Claude Sonnet 4.5 (200K)' },
                { value: 'openai/anthropic/claude-opus-4.5', label: 'Claude Opus 4.5 (200K)' }
            ];
            break;
        case 'openai':
            options = [
                { value: 'openai/openai/gpt-4o-mini', label: 'GPT-4o Mini (128K)' },
                { value: 'openai/openai/gpt-4o', label: 'GPT-4o (128K)' },
                { value: 'openai/openai/gpt-5-mini', label: 'GPT-5 Mini (1M)' },
                { value: 'openai/openai/gpt-5.2', label: 'GPT-5.2 (1M)' }
            ];
            break;
        case 'deepseek':
            options = [
                { value: 'openai/tngtech/deepseek-r1t2-chimera:free', label: 'R1 Chimera (Free) ⭐' },
                { value: 'openai/deepseek/deepseek-chat-v3-0324', label: 'DeepSeek Chat V3 (128K)' },
                { value: 'openai/deepseek/deepseek-v3.2', label: 'DeepSeek V3.2 (128K)' }
            ];
            break;
        case 'meta':
            options = [
                { value: 'openai/meta-llama/llama-3.1-8b-instruct', label: 'Llama 3.1 8B (128K)' },
                { value: 'openai/meta-llama/llama-3.1-405b-instruct:free', label: 'Llama 3.1 405B (Free) ⭐' },
                { value: 'openai/meta-llama/llama-3.1-405b-instruct', label: 'Llama 3.1 405B (128K)' },
                { value: 'openai/meta-llama/llama-3.3-70b-instruct:free', label: 'Llama 3.3 70B (Free) ⭐' },
                { value: 'openai/meta-llama/llama-3.3-70b-instruct', label: 'Llama 3.3 70B (128K)' }
            ];
            break;
        case 'xai':
            options = [
                { value: 'openai/x-ai/grok-code-fast-1', label: 'Grok Code Fast 1 (128K)' },
                { value: 'openai/x-ai/grok-4-fast', label: 'Grok 4 Fast 128K' },
                { value: 'openai/x-ai/grok-vision-1', label: 'Grok Vision 1 (128K)' }
            ];
            break;
        case 'mistral':
            options = [
                { value: 'openai/mistralai/mistral-7b-instruct', label: 'Mistral 7B Instruct (32K)' },
                { value: 'openai/mistralai/mixtral-8x7b-instruct', label: 'Mixtral 8x7B Instruct (32K)' }
            ];
            break;
        case 'xiaomi':
            options = [
                { value: 'openai/xiaomi/minicpm-v-2.6-instruct', label: 'MiniCPM V 2.6 Instruct (32K)' }
            ];
            break;
        default:
            options = [{ value: 'openai/tngtech/deepseek-r1t2-chimera:free', label: 'R1 Chimera (Free) ⭐' }];
    }

    // 옵션 추가
    options.forEach(opt => {
        const option = document.createElement('option');
        option.value = opt.value;
        option.textContent = opt.label;
        modelVersionSelect.appendChild(option);
    });

    // 이전에 저장된 모델 버전 복원
    const savedModelVersion = sessionStorage.getItem(MODEL_VERSION_KEY);
    if (savedModelVersion && Array.from(modelVersionSelect.options).some(opt => opt.value === savedModelVersion)) {
        modelVersionSelect.value = savedModelVersion;
    }

    // 제공사 선택 저장
    sessionStorage.setItem(MODEL_PROVIDER_KEY, provider);
}

// 외부에서 접근 가능하도록 window 객체에 할당
window.scrollToBottom = scrollToBottom;
window.enableGameUI = enableGameUI;
window.disableGameUI = disableGameUI;
window.initializeEmptyGameUI = initializeEmptyGameUI;
window.showEmptyDebugState = showEmptyDebugState;
window.restoreChatLog = restoreChatLog;
window.resetGameUI = resetGameUI;
window.updateStats = updateStats;
window.updateDebugInfo = updateDebugInfo;
window.openLoadModal = openLoadModal;
window.closeLoadModal = closeLoadModal;
window.reloadScenarioList = reloadScenarioList;
window.showToast = showToast;
window.editScenario = editScenario;
window.openScenesView = openScenesView;
window.updateModelVersions = updateModelVersions;
