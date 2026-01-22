/**
 * tutorial.js - TRPG Studio 튜토리얼 시스템
 * 플레이어 모드와 빌더 모드에 대한 가이드를 제공합니다.
 */

const TutorialSystem = (function () {
    let currentStep = 0;
    let tutorialSteps = [];
    let isTutorialActive = false;
    let overlayElement = null;
    let tooltipElement = null;
    let currentHighlightElement = null;

    // 튜토리얼 데이터
    const tutorials = {
        'player': [
            {
                target: '#chat-log',
                text: '환영합니다! 이곳은 게임의 진행 상황과 대화가 표시되는 로그 영역입니다.',
                position: 'bottom'
            },
            {
                target: '#action-input',
                text: '이곳에 당신의 행동이나 대사를 입력하세요. "주변을 둘러본다"라고 입력해볼까요?',
                position: 'top'
            },
            {
                target: '#player-stats-area',
                text: '캐릭터의 상태와 인벤토리, 접속 중인 다른 플레이어들의 정보를 여기서 확인할 수 있습니다.',
                position: 'left'
            },
            {
                target: '#load-btn',
                text: '저장된 시나리오를 불러오거나 새로운 게임을 시작하려면 이 버튼을 누르세요.',
                position: 'bottom'
            }
        ],
        'builder': [
            {
                target: '#builder-left-panel',
                text: '왼쪽 패널에서 시나리오 구성 요소(씬, 엔딩)를 추가하고 관리할 수 있습니다.',
                position: 'right'
            },
            {
                target: '#builder-canvas',
                text: '중앙 캔버스에서 씬들의 흐름을 시각적으로 연결하고 편집합니다.',
                position: 'bottom'
            },
            {
                target: '#builder-right-panel',
                text: '오른쪽 패널에서 선택한 씬의 세부 내용(텍스트, NPC, 이벤트 등)을 수정합니다.',
                position: 'left'
            },
            {
                target: '#builder-publish-btn',
                text: '작업이 완료되면 이 버튼으로 변경사항을 저장하고 플레이 가능한 상태로 만듭니다.',
                position: 'bottom'
            }
        ]
    };

    function init() {
        console.log('[Tutorial] Initialized');
        createOverlay();
    }

    function createOverlay() {
        if (document.getElementById('tutorial-overlay')) return;

        // 배경 오버레이
        overlayElement = document.createElement('div');
        overlayElement.id = 'tutorial-overlay';
        overlayElement.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.7);
            z-index: 9998;
            display: none;
            pointer-events: all;
        `;

        // 툴팁 박스
        tooltipElement = document.createElement('div');
        tooltipElement.id = 'tutorial-tooltip';
        tooltipElement.className = 'pixel-border';
        tooltipElement.style.cssText = `
            position: fixed;
            background: #1A0B2E;
            border: 2px solid #00FFFF;
            padding: 15px;
            color: white;
            z-index: 10000;
            max-width: 300px;
            box-shadow: 0 0 15px rgba(0, 255, 255, 0.5);
            display: none;
            font-family: 'DotGothic16', sans-serif;
        `;

        document.body.appendChild(overlayElement);
        document.body.appendChild(tooltipElement);

        // 오버레이 클릭 시 다음 단계 (선택 사항)
        // overlayElement.addEventListener('click', nextStep);
    }

    let currentMode = null; // 현재 모드 추적

    function start(mode, force = false) {
        console.log(`[Tutorial] Starting tutorial. Mode: ${mode}, Force: ${force}`);
        if (!tutorials[mode]) {
            console.error(`Tutorial mode '${mode}' not found.`);
            return;
        }

        // 이미 완료했고, 강제 실행이 아니면 실행 안 함
        if (!force && localStorage.getItem(`trpg_tutorial_completed_${mode}`) === 'true') {
            return;
        }

        tutorialSteps = tutorials[mode];
        currentStep = 0;
        isTutorialActive = true;
        currentMode = mode;

        overlayElement.style.display = 'block';
        showStep(currentStep);
    }

    async function checkAndStart(mode) {
        console.log(`[Tutorial] Checking status for mode: ${mode}`);
        // 1. 로컬 스토리지 확인
        const isLocallyCompleted = localStorage.getItem(`trpg_tutorial_completed_${mode}`);
        if (isLocallyCompleted === 'true') {
            console.log('[Tutorial] Skipped (Local storage found).');
            return;
        }

        // 2. 서버 DB 확인 (Player 모드인 경우만)
        if (mode === 'player') {
            try {
                const res = await fetch('/api/user/status');
                const data = await res.json();
                if (data.success && data.tutorial_completed) {
                    console.log('[Tutorial] Skipped (Server record found).');
                    // 서버에 이미 완료 기록이 있으면 로컬에도 저장하고 실행 안 함
                    localStorage.setItem(`trpg_tutorial_completed_${mode}`, 'true');
                    return;
                }
            } catch (e) {
                console.warn('Failed to check tutorial status from server:', e);
            }
        }

        // 3. 완료 안 했으면 실행
        start(mode, true);
    }

    async function showStep(stepIndex, retryCount = 0) {
        console.log(`[Tutorial] Showing step ${stepIndex}. Retry: ${retryCount}`);
        if (stepIndex >= tutorialSteps.length) {
            end();
            return;
        }

        const step = tutorialSteps[stepIndex];
        let target = document.querySelector(step.target);

        // [안전성] 타겟 요소가 렌더링될 때까지 잠시 대기 (최대 2.5초)
        // 타겟을 못 찾아도 바로 포기하지 않고 재시도
        if (!target && retryCount < 5) {
            setTimeout(() => showStep(stepIndex, retryCount + 1), 500);
            return;
        }

        // 하이라이트 초기화
        if (currentHighlightElement) {
            currentHighlightElement.style.zIndex = '';
            currentHighlightElement.style.position = '';
            currentHighlightElement.style.boxShadow = '';
            currentHighlightElement.style.outline = '';
            currentHighlightElement.style.borderRadius = '';
            currentHighlightElement.classList.remove('tutorial-highlight');
            currentHighlightElement = null;
        }

        // 툴팁 내용 설정
        tooltipElement.innerHTML = `
            <div style="margin-bottom: 10px; line-height: 1.4;">
                <span style="color: #00FFFF; font-weight: bold;">[STEP ${stepIndex + 1}/${tutorialSteps.length}]</span><br>
                ${step.text}
            </div>
            <div style="text-align: right;">
                <button id="tutorial-next-btn" onclick="window.TutorialSystem.nextStep()" style="
                    background: #00FFFF; color: #000; border: none; padding: 5px 10px; 
                    font-family: inherit; font-weight: bold; cursor: pointer; 
                    border: 2px solid #fff;">
                    ${stepIndex === tutorialSteps.length - 1 ? '완료' : '다음'}
                </button>
                ${stepIndex < tutorialSteps.length - 1 ?
                '<button id="tutorial-skip-btn" onclick="window.TutorialSystem.end()"  style="background:transparent; color:#888; border:none; margin-right:10px; cursor:pointer;">건너뛰기</button>' : ''}
            </div>
        `;

        if (target) {
            console.log(`[Tutorial] Target found:`, target);
            // [정상] 타겟이 있으면 해당 위치에 표시 및 하이라이트
            tooltipElement.style.transform = 'none'; // 중앙 정렬 해제

            target.style.position = 'relative';
            const computedStyle = window.getComputedStyle(target);
            if (computedStyle.position === 'static') target.style.position = 'relative';
            target.style.zIndex = '9999';

            // 시각적 강조
            target.style.boxShadow = '0 0 30px rgba(0, 255, 255, 0.6)';
            target.style.outline = '3px solid #00FFFF';
            target.style.borderRadius = '4px';

            target.classList.add('tutorial-highlight');
            currentHighlightElement = target;

            // 위치 계산
            const rect = target.getBoundingClientRect();
            let top, left;

            if (step.position === 'bottom') { top = rect.bottom + 10; left = rect.left + (rect.width / 2) - 150; }
            else if (step.position === 'top') { top = rect.top - 100; left = rect.left + (rect.width / 2) - 150; }
            else if (step.position === 'left') { top = rect.top; left = rect.left - 320; }
            else if (step.position === 'right') { top = rect.top; left = rect.right + 10; }
            else { top = rect.bottom + 10; left = rect.left; } // Default

            // 화면 보정
            if (left < 10) left = 10;
            if (left + 300 > window.innerWidth) left = window.innerWidth - 320;
            if (top < 10) top = 10;

            const tooltipHeight = tooltipElement.offsetHeight || 150;
            if (top + tooltipHeight > window.innerHeight) {
                top = window.innerHeight - tooltipHeight - 20;
                if (top < 10 && rect.top > tooltipHeight + 20) top = rect.top - tooltipHeight - 10;
            }

            tooltipElement.style.top = `${top}px`;
            tooltipElement.style.left = `${left}px`;
        } else {
            // [Fallback] 타겟이 없으면 화면 중앙에 표시 (적어도 튜토리얼 내용은 보이게)
            console.warn(`Target ${step.target} not found. Showing centered tooltip.`);
            tooltipElement.style.top = '50%';
            tooltipElement.style.left = '50%';
            tooltipElement.style.transform = 'translate(-50%, -50%)';
        }

        tooltipElement.style.display = 'block';
        tooltipElement.style.zIndex = '2147483647';
        overlayElement.style.display = 'block'; // 오버레이 확실히 켜기

        // 버튼 이벤트 연결 (DOM 렌더링 후)
        // Event listeners removed (using inline onclick)
    }

    function nextStep() {
        console.log('[Tutorial] Next step requested.');
        currentStep++;
        showStep(currentStep);
    }

    async function end() {
        console.log('[Tutorial] Ending tutorial.');
        isTutorialActive = false;
        overlayElement.style.display = 'none';
        tooltipElement.style.display = 'none';

        if (currentHighlightElement) {
            currentHighlightElement.style.zIndex = '';
            currentHighlightElement.style.position = '';
            currentHighlightElement.style.boxShadow = '';
            currentHighlightElement.style.outline = '';
            currentHighlightElement.style.borderRadius = '';
            currentHighlightElement.classList.remove('tutorial-highlight');
            currentHighlightElement = null;
        }

        if (currentMode) {
            localStorage.setItem(`trpg_tutorial_completed_${currentMode}`, 'true');

            // Player 모드 완료 시 서버에 저장
            if (currentMode === 'player') {
                try {
                    await fetch('/api/user/tutorial/complete', { method: 'POST' });
                    // showToast("튜토리얼 완료가 서버에 저장되었습니다.", "success");
                } catch (e) {
                    console.error('Failed to update tutorial status on server:', e);
                }
            }
        }
    }

    // UI Manager의 showToast 사용 또는 자체 구현
    function showToast(msg, type) {
        if (window.showToast) {
            window.showToast(msg, type);
        } else {
            console.log(msg);
        }
    }

    return {
        init: init,
        start: start,
        end: end,
        nextStep: nextStep, // end 함수도 노출 필요 (건너뛰기 버튼 등 외부 호출 가능성 대비)
        checkAndStart: checkAndStart
    };
})();

// 안전한 초기화 및 전역 할당
(function initializeTutorial() {
    // 전역 객체에 할당
    window.TutorialSystem = TutorialSystem;

    // 초기화 함수 실행
    const init = () => {
        if (window.TutorialSystem && window.TutorialSystem.init) {
            window.TutorialSystem.init();
        }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
