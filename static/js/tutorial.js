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
        // 1. 로컬 스토리지 확인
        const isLocallyCompleted = localStorage.getItem(`trpg_tutorial_completed_${mode}`);
        if (isLocallyCompleted === 'true') {
            return;
        }

        // 2. 서버 DB 확인 (Player 모드인 경우만)
        if (mode === 'player') {
            try {
                const res = await fetch('/api/user/status');
                const data = await res.json();
                if (data.success && data.tutorial_completed) {
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

    function showStep(stepIndex) {
        if (stepIndex >= tutorialSteps.length) {
            end();
            return;
        }

        const step = tutorialSteps[stepIndex];
        const target = document.querySelector(step.target);

        // 하이라이트 초기화
        if (currentHighlightElement) {
            currentHighlightElement.style.zIndex = '';
            currentHighlightElement.style.position = '';
            currentHighlightElement.classList.remove('tutorial-highlight');
        }

        if (target) {
            // 요소 하이라이트
            target.style.position = 'relative'; // z-index 적용을 위해 relative 필요
            const computedStyle = window.getComputedStyle(target);
            if (computedStyle.position === 'static') {
                target.style.position = 'relative';
            }
            target.style.zIndex = '9999';
            target.classList.add('tutorial-highlight');
            currentHighlightElement = target;

            // 툴팁 위치 계산 (최적화)
            const rect = target.getBoundingClientRect();
            let top, left;

            tooltipElement.innerHTML = `
                <div style="margin-bottom: 10px; line-height: 1.4;">
                    <span style="color: #00FFFF; font-weight: bold;">[STEP ${stepIndex + 1}/${tutorialSteps.length}]</span><br>
                    ${step.text}
                </div>
                <div style="text-align: right;">
                    <button id="tutorial-next-btn" style="
                        background: #00FFFF; 
                        color: #000; 
                        border: none; 
                        padding: 5px 10px; 
                        font-family: inherit; 
                        font-weight: bold; 
                        cursor: pointer;
                        border: 2px solid #fff;
                    ">${stepIndex === tutorialSteps.length - 1 ? '완료' : '다음'}</button>
                    ${stepIndex < tutorialSteps.length - 1 ? '<button id="tutorial-skip-btn" style="background:transparent; color:#888; border:none; margin-right:10px; cursor:pointer;">건너뛰기</button>' : ''}
                </div>
            `;

            tooltipElement.style.display = 'block';

            // 위치 계산 로직 유지
            if (step.position === 'bottom') {
                top = rect.bottom + 10;
                left = rect.left + (rect.width / 2) - 150;
            } else if (step.position === 'top') {
                top = rect.top - 100; // 예상 높이 감안
                left = rect.left + (rect.width / 2) - 150;
            } else if (step.position === 'left') {
                top = rect.top;
                left = rect.left - 320;
            } else if (step.position === 'right') {
                top = rect.top;
                left = rect.right + 10;
            }

            // 화면 밖으로 나가지 않게 보정
            if (left < 10) left = 10;
            if (left + 300 > window.innerWidth) left = window.innerWidth - 320;
            if (top < 10) top = 10;
            if (top + 100 > window.innerHeight) top = window.innerHeight - 120; // 하단 보정

            tooltipElement.style.top = `${top}px`;
            tooltipElement.style.left = `${left}px`;

            // 버튼 이벤트 연결
            const nextBtn = document.getElementById('tutorial-next-btn');
            nextBtn.onclick = nextStep;

            const skipBtn = document.getElementById('tutorial-skip-btn');
            if (skipBtn) skipBtn.onclick = end;

        } else {
            console.warn(`Target ${step.target} not found, skipping step.`);
            nextStep();
        }
    }

    function nextStep() {
        currentStep++;
        showStep(currentStep);
    }

    async function end() {
        isTutorialActive = false;
        overlayElement.style.display = 'none';
        tooltipElement.style.display = 'none';

        if (currentHighlightElement) {
            currentHighlightElement.style.zIndex = '';
            currentHighlightElement.classList.remove('tutorial-highlight');
            currentHighlightElement = null;
        }

        // 완료 시 로컬 스토리지 및 서버에 저장
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
        checkAndStart: checkAndStart
    };
})();

// 페이지 로드 시 초기화
document.addEventListener('DOMContentLoaded', () => {
    TutorialSystem.init();
    window.TutorialSystem = TutorialSystem;
});
