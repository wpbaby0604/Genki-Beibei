const videoElement = document.getElementById('input_video');

// ============================================================
// 🎥 展示用攝影機預覽窗（Demo 用：讓觀眾看到閉眼 3 秒的偵測過程）
//    開啟方式一：Python 端 render_beibei(..., show_camera=True)
//    開啟方式二：網址列加 ?cam=1
//    兩者都沒有時完全隱藏，不影響一般使用者。
// ============================================================
const camBox    = document.getElementById('cam_box');
const camStatus = document.getElementById('cam_status');
const camBar    = document.getElementById('cam_bar');

function setCameraPreview(on) {
    if (!camBox) return;
    camBox.classList.toggle('on', !!on);
}
// 網址參數可強制打開，方便臨時 Demo
try {
    if (new URLSearchParams(location.search).get('cam') === '1') setCameraPreview(true);
} catch (e) {}

// 由偵測迴圈每幀呼叫，更新數值 / 進度條 / 顏色
function updateCamStatus(avgEAR, closedMs, fired) {
    if (!camBox || !camBox.classList.contains('on')) return;
    const pct = Math.max(0, Math.min(100, (closedMs / DROWSY_TIME_LIMIT) * 100));
    camBar.style.width = pct + '%';
    camStatus.classList.remove('warn', 'alert');
    if (fired) {
        camStatus.classList.add('alert');
        camStatus.textContent = '😴 已觸發關心！';
    } else if (closedMs > 0) {
        camStatus.classList.add('warn');
        camStatus.textContent = `閉眼 ${(closedMs / 1000).toFixed(1)}s / 3.0s`;
    } else {
        camStatus.textContent = `👁 EAR ${avgEAR.toFixed(3)}（門檻 ${DROWSY_THRESHOLD}）`;
    }
}
const canvasElement = document.getElementById('live2d_canvas');
let beibeiModel = null;
let audioContext = null;
let analyser = null;

// 🌟 修正 1：補上這三個靈魂變數！
let currentPlayingAudio = null;  // 用來記錄目前正在播放的聲音
let currentFaceAngleX = 0;       // 用來記錄 VR 模式使用者的臉部 X 軸
let currentFaceAngleY = 0;       // 用來記錄 VR 模式使用者的臉部 Y 軸

// 🌟 記錄「最後一次播過的音訊指紋」，避免 Streamlit 因縮放/收側邊欄重繪
//    而把同一段語音重播。只有指紋變了（= 真的有新語音）才會再播一次。
let lastPlayedAudioSig = null;

let currentCompanionMode = 1;   // 🌟 記錄目前模式，給 onResults 即時讀取
let beibeiGrid = [];            // 🌟 角度網格 base64 陣列（已含 data: 前綴）
let beibeiGridDim = 7;          // 🌟 網格邊長 N

let smoothFaceAngleX = 0;   // 🌟 濾平後的臉部角度（給選格用）
let smoothFaceAngleY = 0;
const FACE_SMOOTH = 0.18;   // 0~1：越小越平滑但反應慢，越大越靈敏但較抖（原本 0.25，調低換取更穩定的追蹤）

// 🌟 新增：5分鐘主動搭話的專屬變數
let idleTimeout = null;
const IDLE_TIME_LIMIT = 5 * 60 * 1000; // 5 分鐘 = 300,000 毫秒

// 🌟 新增：打瞌睡偵測的專屬變數
let drowsyStartTime = null;
const DROWSY_THRESHOLD = 0.2; // 閉眼判定標準 (跟妳 Python 端一樣)
const DROWSY_TIME_LIMIT = 3000; // 閉眼超過 3 秒 (3000毫秒) 就叫醒

// 🌟 待機排程：平常安靜「發呆(idle)」佔大多數時間，偶爾才插一個「只做一次」的小動作，哈欠很稀有。
let idleNextSwitchAt = 0;               // 下一次該重新抽段的時間戳 (Date.now() 毫秒)
let idleClipSrc = null;                 // 目前待機正在播的那段圖 (base64 src)
let idleClipLabel = 'idle（發呆）';      // 左上角除錯標籤文字
let idlePool = [];                      // 🌟 只放「偶爾的小動作」：偏頭/飄眼/點頭（不含平常的 idle）
let yawnCooldownUntil = 0;              // 🌟 打完哈欠後，這個時間點之前不再打（避免狂打哈欠）

// 🌟 待機小動作的「次數控制」設定
//    ms = 該段 clip 長度(毫秒) = 幀數 × 每幀毫秒，必須對齊 bake 腳本的 ACTION_FRAMES！
//    min/max = 這個動作一次要重複幾次(隨機落在範圍內)，播完該次數就換回發呆。
const IDLE_MOTION_CFG = {
    idle_tilt:   { ms: 40 * 110, min: 1, max: 2 },  // 偏頭 1~2 次
    idle_glance: { ms: 36 * 110, min: 1, max: 2 },  // 飄眼 1~2 次
    idle_nod:    { ms: 32 * 110, min: 1, max: 2 },  // 點頭 1~2 次
};
const YAWN_MS = 28 * 110;               // 哈欠 clip 長度；固定只播 1 次
let forcedAction = "";                  // 🔧 除錯：被強制顯示的表情/動作（""＝正常自動）
let clipMap = {};                       // 🔧 動作名稱 → base64 src 的對照表，給強制顯示查用

// 🌟 新增：重置沙漏的控制功能
function resetIdleTimer() {
    if (idleTimeout) clearTimeout(idleTimeout);
    idleTimeout = setTimeout(() => {
        console.log("主人 5 分鐘沒理貝貝了，發送主動搭話訊號！");
        const idleValue = "idle_" + Date.now();
        window.parent.postMessage({
            isStreamlitMessage: true,
            type: "streamlit:setComponentValue",
            value: idleValue
        }, "*");
    }, IDLE_TIME_LIMIT);
}

// ==========================================
// 1. 初始化 PIXI 與 Live2D 模型
// ==========================================
const app = new PIXI.Application({
    view: canvasElement,
    transparent: true,
    autoStart: true,
    resizeTo: window
});

// 🟢 新增：設定 canvas 的 CSS cursor
canvasElement.style.cursor = 'pointer';

PIXI.live2d.Live2DModel.from('./assets/rice_pc_pro_t02.model3.json').then(model => {
    app.stage.addChild(model);
    beibeiModel = model;
    // --- 新增：互動功能 ---
    model.interactive = true; // 開啟互動開關
    model.buttonMode = true;  // 滑鼠移上去會變成小手狀

    // 當點擊模型時觸發
    model.on('pointertap', () => {
        resetIdleTimer(); // 👈 戳她的時候，也要重新計算 5 分鐘喔！
        console.log("妳點到貝貝了！");
        
        // 1. 讓她動一下 (原本就有)
        if (model.motion) model.motion('Tap'); 

        // 2. 🌟 雙重發送：確保 Streamlit 收到訊息
        const clickValue = "clicked_" + Date.now(); // 加上時間戳，確保每次內容都不同
        
        // 方法 A：官方標準做法
        if (window.Streamlit) {
            window.Streamlit.setComponentValue(clickValue);
        }
        
        // 方法 B：底層強制發送
        window.parent.postMessage({
            isStreamlitMessage: true,
            type: "streamlit:setComponentValue",
            value: clickValue
        }, "*");
    });

    model.anchor.set(0.5, 0.5); // 錨點設在中心
    
    // 1. 縮小比例：原本是 0.1，如果太大只看到腳，試著改成 0.05 或更小
    model.scale.set(0.2); 

    // 2. 調整位置：
    // x 放在畫布中間
    model.x = app.screen.width / 2 + 250; 
    // y 往下拉一點：如果現在只看到腳，代表模型太高了，我們要把 y 值增加
    // 妳可以根據實際畫面微調這個數字 (例如從原本的 height / 2 改成下面這樣)
    model.y = app.screen.height / 2 + 400;

    console.log("小米模型載入成功！");
    // 🌟 核心修正：手動發送「我準備好了」的訊號給 Streamlit
    // 既然原本的 function 不給用，我們直接用底層的 postMessage
    window.parent.postMessage({
        isStreamlitMessage: true,
        type: "streamlit:componentReady",
        apiVersion: 1
    }, "*");

    // 設定畫布高度，確保畫面出得來
    window.parent.postMessage({
        isStreamlitMessage: true,
        type: "streamlit:setFrameHeight",
        height: 600
    }, "*");

}).catch(e => {
    console.error("模型載入失敗：", e);
});

// ==========================================
// 2. 語音播放與唇同步系統 (Lip-Sync)
// ==========================================
function playAudioAndSyncLip(base64Audio) {
    if (!audioContext) {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
        analyser = audioContext.createAnalyser();
    }
// 🌟 修正 2：如果上一句話還沒講完，先強制卡掉，避免兩句話疊在一起
    if (currentPlayingAudio) {
        currentPlayingAudio.pause();
    }

    
    // 🌟 修正 2：把這句話存進全域變數，讓底下的狀態機能讀取！
    const audio = new Audio("data:audio/mp3;base64," + base64Audio);

    currentPlayingAudio = audio;

    // 🌟 語音播完就把嘴巴停下來、切回 idle（不要一直循環動）
    audio.onended = () => {
        if (currentPlayingAudio !== audio) return;          // 已被新語音取代就不管
        const ai = document.getElementById('ai_beibei_img');
        if (ai && ai.style.display !== 'none' && ai.dataset.idleB64) {
            // 模式二的跟臉由 onResults 每幀接手，這裡只負責把模式一/待機切回 idle
            if (!(currentCompanionMode === 2 && beibeiGrid.length > 0)) {
                setAiFrame(ai.dataset.idleB64);
            }
        }
    };

    
    const source = audioContext.createMediaElementSource(audio);
    source.connect(analyser);
    analyser.connect(audioContext.destination);

    // 嘗試播放聲音，並捕捉被瀏覽器擋住的錯誤
    audio.play().then(() => {
        console.log("聲音播放成功！");
    }).catch(error => {
        console.error("聲音被瀏覽器擋住了！請用滑鼠點擊一下貝貝的畫面：", error);
    });

    function updateMouth() {
        if (!audio.paused && !audio.ended) {
            requestAnimationFrame(updateMouth);

            const dataArray = new Uint8Array(analyser.frequencyBinCount);
            analyser.getByteFrequencyData(dataArray);

            let sum = 0;
            for (let i = 0; i < dataArray.length; i++) {
                sum += dataArray[i];
            }
            let volume = sum / dataArray.length;
            let mouthOpen = Math.min(volume / 40, 1.0);

            if (beibeiModel) {
                beibeiModel.internalModel.coreModel.setParameterValueById('ParamMouthOpenY', mouthOpen);
            }
        } else {
            if (beibeiModel) {
                beibeiModel.internalModel.coreModel.setParameterValueById('ParamMouthOpenY', 0);
            }
        }
    }
    updateMouth();
}

// ==========================================
// 3. MediaPipe 臉部追蹤與打瞌睡連動
// ==========================================
// 🌟 新增：計算兩點距離與 EAR 的數學公式
function getDistance(p1, p2) {
    return Math.hypot(p1.x - p2.x, p1.y - p2.y);
}
function calculateEAR(landmarks, indices) {
    const p0 = landmarks[indices[0]], p1 = landmarks[indices[1]], p2 = landmarks[indices[2]];
    const p3 = landmarks[indices[3]], p4 = landmarks[indices[4]], p5 = landmarks[indices[5]];
    const v1 = getDistance(p1, p5);
    const v2 = getDistance(p2, p4);
    const h = getDistance(p0, p3);
    if (h === 0) return 0;
    return (v1 + v2) / (2.0 * h);
}

// 🌟 把即時臉部角度對應到最接近的網格格子（row-major：row=y, col=x）
const GRID_INPUT_RANGE = 18;   // 把 currentFaceAngleX/Y 視為 ±18 度範圍（依實測微調）
const GRID_FLIP_X = false;     // 若左右相反，改成 true
const GRID_FLIP_Y = false;     // 若上下相反，改成 true
let currentEmotionLabel = "neutral";   // 🆕 從 Python 端傳來的目前情緒（happy/sad/angry/neutral）
function setDebugLabel(text) {
    const el = document.getElementById('beibei_debug_label');
    if (el) el.textContent = "狀態：" + text;
}

// 🌟 換幀輔助函式：讓 aiImg 換圖時用交叉淡出取代硬切換，減少「跳格子」的頓挫感。
// 新畫面先在上層淡入層裡淡入，淡入完成後才「歸位」到底層 aiImg 本身，
// 這樣 dataset/onclick/display 這些狀態邏輯完全不用動，只是換圖的視覺效果變順了。
let _aiFadeTimer = null;
function setAiFrame(newSrc) {
    const base = document.getElementById('ai_beibei_img');
    const fade = document.getElementById('ai_beibei_img_fade');
    if (!base || !newSrc || base.src === newSrc) return;
    if (!fade) { base.src = newSrc; return; }   // 保底：淡入層還沒建立好就直接切換

    if (_aiFadeTimer) clearTimeout(_aiFadeTimer);
    fade.src = newSrc;
    fade.style.display = base.style.display;
    // 用 rAF 確保瀏覽器先畫出 opacity:0 的起始狀態，transition 才會真的播放
    requestAnimationFrame(() => { fade.style.opacity = '1'; });
    _aiFadeTimer = setTimeout(() => {
        base.src = newSrc;
        fade.style.opacity = '0';
    }, 130);
}

function faceAngleToGridIndex(ax, ay) {
    const N = beibeiGridDim;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    let nx = (clamp(ax, -GRID_INPUT_RANGE, GRID_INPUT_RANGE) + GRID_INPUT_RANGE) / (2 * GRID_INPUT_RANGE);
    let ny = (clamp(ay, -GRID_INPUT_RANGE, GRID_INPUT_RANGE) + GRID_INPUT_RANGE) / (2 * GRID_INPUT_RANGE);
    if (GRID_FLIP_X) nx = 1 - nx;
    if (GRID_FLIP_Y) ny = 1 - ny;
    const col = Math.round(nx * (N - 1));
    const row = Math.round(ny * (N - 1));
    return row * N + col;
}

const faceMesh = new FaceMesh({
    locateFile: (file) => `https://cdn.jsdelivr.net/npm/@mediapipe/face_mesh/${file}`
});
faceMesh.setOptions({ maxNumFaces: 1, refineLandmarks: true, minDetectionConfidence: 0.5 });

faceMesh.onResults((results) => {
    if (!results.multiFaceLandmarks || results.multiFaceLandmarks.length === 0 || !beibeiModel) return;
    const landmarks = results.multiFaceLandmarks[0];

    // --- 1. 頭部轉動追蹤 ---
    const nose = landmarks[1];
    // 🌟 修正 3：把原本的 const 改成我們上方定義好的全域變數
    currentFaceAngleX = (nose.x - 0.5) * -60;
    currentFaceAngleY = (nose.y - 0.5) * -60;
        // 🌟 把抖動的角度濾平，避免在格子邊界來回閃
    smoothFaceAngleX += (currentFaceAngleX - smoothFaceAngleX) * FACE_SMOOTH;
    smoothFaceAngleY += (currentFaceAngleY - smoothFaceAngleY) * FACE_SMOOTH;
    // ✅ 修正：用 currentFaceAngleX/Y，不是未定義的 angleX/angleY
    beibeiModel.internalModel.coreModel.setParameterValueById('ParamAngleX', currentFaceAngleX);
    beibeiModel.internalModel.coreModel.setParameterValueById('ParamAngleY', currentFaceAngleY);

    // --- 2. 🌟 疲勞/打瞌睡偵測 (EAR) ---
    const leftEyeIndices = [33, 160, 158, 133, 153, 144];
    const rightEyeIndices = [362, 385, 387, 263, 373, 380];
    const leftEAR = calculateEAR(landmarks, leftEyeIndices);
    const rightEAR = calculateEAR(landmarks, rightEyeIndices);
    const avgEAR = (leftEAR + rightEAR) / 2;

    // ✅ 修正：眼睛開合同步（EAR 正常約 0.28，乘以 3.5 映射到 0~1）
    beibeiModel.internalModel.coreModel.setParameterValueById('ParamEyeLOpen', Math.min(leftEAR * 3.5, 1.0));
    beibeiModel.internalModel.coreModel.setParameterValueById('ParamEyeROpen', Math.min(rightEAR * 3.5, 1.0));



    // 🌟 選項二：上傳照片(THA3)的模式二跟臉 —— 依即時角度即時換網格幀
    if (currentCompanionMode === 2 && beibeiGrid.length > 0) {
        const aiImg = document.getElementById('ai_beibei_img');
        if (aiImg && aiImg.style.display !== 'none') {
            const speaking = (currentPlayingAudio && !currentPlayingAudio.paused);
            const drowsy = (drowsyStartTime && Date.now() - drowsyStartTime > DROWSY_TIME_LIMIT);
            if (!speaking && !drowsy && !forcedAction) {   // 說話/打瞌睡/除錯強制優先，其餘時間才跟臉
                const src = beibeiGrid[faceAngleToGridIndex(smoothFaceAngleX, smoothFaceAngleY)];
                setAiFrame(src);
            }
        }
    }
    // 核心邏輯：如果低於標準，開始計時
    let _camClosedMs = 0, _camFired = false;   // 🎥 給預覽窗顯示用
    if (avgEAR < DROWSY_THRESHOLD) {
        if (drowsyStartTime) _camClosedMs = Math.max(0, Date.now() - drowsyStartTime);
        if (!drowsyStartTime) {
            drowsyStartTime = Date.now(); // 剛閉上眼睛，記下時間
        } else if (Date.now() - drowsyStartTime > DROWSY_TIME_LIMIT) {
            _camFired = true;
            // 🌟 閉眼超過 3 秒！觸發警報！
            console.log("偵測到打瞌睡！發送警告給 Python！");
            
            // 發送專屬的 drowsy 訊號給 Streamlit
            const warningValue = "drowsy_" + Date.now();
            
            // 🌟 關鍵修正：直接使用「底層強制發送」(方法 B)，確保 Python 絕對不漏接！
            window.parent.postMessage({
                isStreamlitMessage: true, 
                type: "streamlit:setComponentValue", 
                value: warningValue
            }, "*");
            
            // 設定 10 秒冷卻時間，避免貝貝連續瘋狂叫妳
            drowsyStartTime = Date.now() + 10000; 
        }
    } else {
        drowsyStartTime = null; // 眼睛睜開就重置計時器
    }
    updateCamStatus(avgEAR, _camClosedMs, _camFired);   // 🎥 更新預覽窗
});

const camera = new Camera(videoElement, {
    onFrame: async () => { await faceMesh.send({ image: videoElement }); },
    width: 640, height: 480
});
camera.start();

// ==========================================
// ==========================================
// 🌟 待機排程：抽成獨立函式，並用 setInterval 每 0.4 秒持續驅動。
//    原因：待機切換原本只寫在 onRender 裡，而 onRender 只有 Streamlit rerun 時才觸發，
//    使用者不互動時就很久不跑一次 → 待機會「一直卡在發呆、不切換」。改用計時器就會持續切換。
// ==========================================
function runIdleScheduler() {
    const aiImg = document.getElementById('ai_beibei_img');
    if (!aiImg || aiImg.style.display === 'none') return;
    if (currentCompanionMode !== 1) return;              // 只管模式一待機
    if (forcedAction) return;                            // 除錯強制時不插手
    const speaking = (currentPlayingAudio && !currentPlayingAudio.paused);
    const drowsy = (drowsyStartTime && Date.now() - drowsyStartTime > DROWSY_TIME_LIMIT);
    if (speaking || drowsy) return;                      // 講話 / 打瞌睡優先，不插手
    if (Date.now() >= idleNextSwitchAt) {
        const r = Math.random();
        const canYawn = !!aiImg.dataset.yawnB64 && Date.now() >= yawnCooldownUntil;
        if (canYawn && r < 0.06) {
            idleClipSrc = aiImg.dataset.yawnB64;
            idleClipLabel = 'yawn（打哈欠 ×1）';
            idleNextSwitchAt = Date.now() + YAWN_MS + 150;
            yawnCooldownUntil = Date.now() + 45000;
        } else if (idlePool.length > 0 && r < 0.32) {
            const pick = idlePool[Math.floor(Math.random() * idlePool.length)];
            const times = pick.min + Math.floor(Math.random() * (pick.max - pick.min + 1));
            idleClipSrc = pick.srcs[Math.floor(Math.random() * pick.srcs.length)];  // 飄眼會隨機挑左/右
            idleClipLabel = pick.label + ' ×' + times;
            idleNextSwitchAt = Date.now() + pick.ms * times + 120;
        } else {
            idleClipSrc = aiImg.dataset.idleB64;
            idleClipLabel = 'idle（發呆）';
            idleNextSwitchAt = Date.now() + (12000 + Math.random() * 10000);  // 12~22 秒
        }
    }
    setAiFrame(idleClipSrc || aiImg.dataset.idleB64);
    setDebugLabel(idleClipLabel);
}
// 每 0.4 秒驅動一次；用旗標避免元件重載時重複註冊多個計時器。
if (!window.__beibeiIdleTimer) {
    window.__beibeiIdleTimer = setInterval(runIdleScheduler, 400);
}

// ==========================================
// 4. Streamlit 通訊（正確做法）
// ==========================================
// ✅ 修正：使用 Streamlit.onRender() 而非手動 addEventListener
//    onRender() 內部會自動送出 streamlit:componentReady 給父視窗
//    這樣 Streamlit 才知道元件已就緒，不會顯示錯誤訊息
Streamlit.onRender(function(args) {

    // 🎥 Demo 用攝影機預覽窗開關（由 Python 端 show_camera 控制）
    if (args && args.show_camera !== undefined) {
        setCameraPreview(args.show_camera);
    }

    // 🌟 只要 Python 傳新資料過來（表示剛聊完天或剛換背景），就重新計算 5 分鐘！
    resetIdleTimer();
    Streamlit.setFrameHeight(600);

    if (args && args.audio_data) {
        // 🌟 用「長度 + 開頭片段」當指紋。縮放/收側邊欄會讓 onRender 帶著
        //    同一段 audio_data 再跑一次，指紋相同 → 跳過，就不會重播。
        const sig = args.audio_data.length + ":" + args.audio_data.slice(0, 32);
        if (sig !== lastPlayedAudioSig) {
            lastPlayedAudioSig = sig;
            console.log("收到來自 Python 的『新』語音資料，準備播放！");
            playAudioAndSyncLip(args.audio_data);
        } else {
            console.log("同一段語音（重繪造成），略過不重播。");
        }
    }

    // 🌟 3. 接收 Python 傳來的 bg_image 並鋪上背景
    if (args && args.bg_image !== undefined) {
        const canvas = document.getElementById('live2d_canvas');
        if (canvas) {
            canvas.style.position = 'absolute';
            canvas.style.top = '0';
            canvas.style.left = '0';
            canvas.style.zIndex = '10'; // 確保貝貝浮在最上層
        }

        if (args.bg_image === "") {
            document.body.style.backgroundImage = "none";
        } else {
            // 抓取 assets 資料夾裡的 my_background.jpg
            document.body.style.backgroundImage = `url('./assets/${args.bg_image}')`;
            document.body.style.backgroundSize = "cover";      
            document.body.style.backgroundPosition = "center"; 
            document.body.style.backgroundRepeat = "no-repeat"; 
        }
        
        document.body.style.margin = "0";
        document.body.style.height = "100vh";
        document.body.style.overflow = "hidden";
    }
    // 🌟 新增：處理 AI 模式的動圖與點擊神經
// 🌟 處理 AI 模式的動圖與狀態切換
    if (args && args.ai_webp) {
        const canvas = document.getElementById('live2d_canvas');
        if(canvas) canvas.style.display = 'none';

        let aiImg = document.getElementById('ai_beibei_img');
        if (!aiImg) {
            aiImg = document.createElement('img');
            aiImg.id = 'ai_beibei_img';
            document.body.appendChild(aiImg);
        }
        // 🌟 新增：疊在上面的淡入層，專門用來做換幀的交叉淡出，
        //          本身不承載任何狀態（dataset/onclick 一律留在 aiImg 上）
        let aiImgFade = document.getElementById('ai_beibei_img_fade');
        if (!aiImgFade) {
            aiImgFade = document.createElement('img');
            aiImgFade.id = 'ai_beibei_img_fade';
            aiImgFade.style.pointerEvents = 'none';
            aiImgFade.style.opacity = '0';
            aiImgFade.style.transition = 'opacity 0.12s linear';
            document.body.appendChild(aiImgFade);
        }
        // 🆕 除錯用標籤：顯示目前實際播放的狀態，方便測試時對照表情有沒有問題
        let debugLabel = document.getElementById('beibei_debug_label');
        if (!debugLabel) {
            debugLabel = document.createElement('div');
            debugLabel.id = 'beibei_debug_label';
            debugLabel.style.position = 'fixed';
            debugLabel.style.top = '8px';
            debugLabel.style.left = '8px';
            debugLabel.style.zIndex = '10001';
            debugLabel.style.padding = '4px 10px';
            debugLabel.style.borderRadius = '6px';
            debugLabel.style.background = 'rgba(0,0,0,0.55)';
            debugLabel.style.color = '#fff';
            debugLabel.style.fontFamily = 'monospace';
            debugLabel.style.fontSize = '12px';
            debugLabel.style.pointerEvents = 'none';
            document.body.appendChild(debugLabel);
        }
        // 🌟 動態讀取 Python 傳來的參數 (若無則使用預設值)
        let scale = args.model_scale !== undefined ? args.model_scale : 1.0;
        let offsetX = args.model_x !== undefined ? args.model_x : 0;
        let offsetY = args.model_y !== undefined ? args.model_y : 0;

        // 🌟 覆寫 CSS 樣式，加入動態計算
        aiImg.style.height = (650 * scale) + 'px'; // 根據縮放調整高度
        aiImg.style.width = 'auto';
        aiImg.style.objectFit = 'contain';
        aiImg.style.cursor = 'pointer'; 
        aiImg.style.position = 'absolute';
        
        // 向上平移: 原本底部距離 100px，加上 offsetY
        aiImg.style.bottom = (100 + offsetY) + 'px'; 
        
        // 左右平移: CSS transform 的 calc 計算
        aiImg.style.left = '50%'; 
        aiImg.style.transform = `translateX(calc(-50% + ${offsetX}px))`;
        
        aiImg.style.zIndex = '9999'; 
        aiImg.style.pointerEvents = 'auto';

        // 🌟 淡入層完全比照主圖層的定位，只疊在正上方（zIndex +1），負責交叉淡出
        aiImgFade.style.height = aiImg.style.height;
        aiImgFade.style.width = aiImg.style.width;
        aiImgFade.style.objectFit = 'contain';
        aiImgFade.style.position = 'absolute';
        aiImgFade.style.bottom = aiImg.style.bottom;
        aiImgFade.style.left = '50%';
        aiImgFade.style.transform = aiImg.style.transform;
        aiImgFade.style.zIndex = '10000';

            // 🌟 補回觸覺神經！點擊圖片直接發送訊號給 Python
        aiImg.onclick = () => {
            resetIdleTimer();
            const clickValue = "clicked_" + Date.now();
            window.parent.postMessage({
                isStreamlitMessage: true,
                type: "streamlit:setComponentValue",
                value: clickValue
            }, "*");
        };
            document.body.appendChild(aiImg);
        

        // 🌟 解析 Python 傳來的「四合一」微電影包裝
        try {
            if (args.ai_webp.startsWith('{')) {
                const data = JSON.parse(args.ai_webp);
                aiImg.dataset.idleB64 = "data:image/webp;base64," + data.idle;
                aiImg.dataset.talkingB64 = "data:image/webp;base64," + data.talking;
                aiImg.dataset.yawnB64 = "data:image/webp;base64," + data.yawn;
                aiImg.dataset.alertB64 = "data:image/webp;base64," + data.alert;

                // 🌟 組「小動作池」：只放偏頭/飄眼/點頭（平常的 idle 不放進來，另外處理）。
                //    舊頭像沒有這些 key（膠水層會回空字串），length===0 就不放 → 向下相容。
                idlePool = [];
                // 偏頭、點頭：各一支
                for (const [key, b64, label] of [
                    ['idle_tilt', data.idle_tilt, 'idle_tilt（偏頭）'],
                    ['idle_nod',  data.idle_nod,  'idle_nod（點頭）'],
                ]) {
                    if (b64 && b64.length > 0) {
                        const cfg = IDLE_MOTION_CFG[key];
                        idlePool.push({
                            srcs: ["data:image/webp;base64," + b64], label, key,
                            ms: cfg.ms, min: cfg.min, max: cfg.max,
                        });
                    }
                }
                // 飄眼：在池子裡佔「一格」，但握有左/右兩支，每次隨機挑一支 → 每次不一定往哪邊飄。
                const glanceSrcs = [];
                if (data.idle_glance  && data.idle_glance.length)  glanceSrcs.push("data:image/webp;base64," + data.idle_glance);
                if (data.idle_glance2 && data.idle_glance2.length) glanceSrcs.push("data:image/webp;base64," + data.idle_glance2);
                if (glanceSrcs.length > 0) {
                    const cfg = IDLE_MOTION_CFG['idle_glance'];
                    idlePool.push({
                        srcs: glanceSrcs, label: 'idle_glance（飄眼）', key: 'idle_glance',
                        ms: cfg.ms, min: cfg.min, max: cfg.max,
                    });
                }

                // 🔧 組「動作名稱 → 圖」對照表，給側邊欄的「強制表情/動作」查用。
                clipMap = {
                    idle:    aiImg.dataset.idleB64,
                    talking: aiImg.dataset.talkingB64,   // ＝目前情緒對應的說話表情
                    yawn:    aiImg.dataset.yawnB64,
                    alert:   aiImg.dataset.alertB64,
                };
                if (data.idle_tilt && data.idle_tilt.length)     clipMap.idle_tilt   = "data:image/webp;base64," + data.idle_tilt;
                if (data.idle_glance && data.idle_glance.length) clipMap.idle_glance = "data:image/webp;base64," + data.idle_glance;
                if (data.idle_glance2 && data.idle_glance2.length) clipMap.idle_glance2 = "data:image/webp;base64," + data.idle_glance2;
                if (data.idle_nod && data.idle_nod.length)       clipMap.idle_nod    = "data:image/webp;base64," + data.idle_nod;

                // 🌟 把角度網格存進全域，交給 onResults 用
                if (data.grid && data.grid.length > 0) {
                    beibeiGrid = data.grid.map(b => "data:image/webp;base64," + b);
                    beibeiGridDim = data.gridDim || 5;
                    // 🌟 預先解碼全部網格圖，之後換圖就不會頓
                    beibeiGrid.forEach(src => { const im = new Image(); im.src = src; });
                }
            } else {
                // 🌟 防呆機制：如果收到的不是包裹，就當作一般單圖處理，避免 undefined！
                aiImg.dataset.idleB64 = "data:image/webp;base64," + args.ai_webp;
                aiImg.dataset.talkingB64 = "data:image/webp;base64," + args.ai_webp;
                aiImg.dataset.yawnB64 = "data:image/webp;base64," + args.ai_webp;
                aiImg.dataset.alertB64 = "data:image/webp;base64," + args.ai_webp;
                idlePool = [];  // 單圖時沒有小動作，平常就是那張 idle
                clipMap = {     // 🔧 單圖時所有動作都指向同一張
                    idle:    aiImg.dataset.idleB64,
                    talking: aiImg.dataset.talkingB64,
                    yawn:    aiImg.dataset.yawnB64,
                    alert:   aiImg.dataset.alertB64,
                };
            }
        } catch(e) { console.log("解析動畫 JSON 失敗", e); }

 // 🌟 同步目前模式給 onResults 用（新增這行）
        currentCompanionMode = (typeof args.companion_mode !== 'undefined') ? args.companion_mode : 1;
        currentEmotionLabel = (typeof args.emotion !== 'undefined' && args.emotion) ? args.emotion : "neutral";
        // 🔧 除錯：被強制顯示的表情/動作（""＝正常）。給 onResults 也讀得到，所以存全域。
        forcedAction = (typeof args.forced_action !== 'undefined' && args.forced_action) ? args.forced_action : "";

        // 🌟 狀態機切換邏輯
        let isSpeaking = (currentPlayingAudio && !currentPlayingAudio.paused);
        let isDrowsyAlert = (drowsyStartTime && Date.now() - drowsyStartTime > DROWSY_TIME_LIMIT);

        if (forcedAction && clipMap[forcedAction]) {
            // 🔧 除錯強制：蓋過一切，一直保持這個表情，直到側邊欄選回「無」
            setAiFrame(clipMap[forcedAction]);
            setDebugLabel("🔧 強制：" + forcedAction);
        } else if (isSpeaking) {
            // 講話中：強制動嘴巴
            setAiFrame(aiImg.dataset.talkingB64);
            setDebugLabel("talk_" + currentEmotionLabel);
        } else if (isDrowsyAlert) {
            // 打瞌睡：強制播放驚醒動作
            setAiFrame(aiImg.dataset.alertB64);
            setDebugLabel("alert（打瞌睡警報）");
        } else if (currentCompanionMode === 1) {
            // 🎭 模式一：待機。實際切換由持續運作的 runIdleScheduler()(setInterval)驅動；
            //            這裡也呼叫一次，確保 rerun 當下能即時反映。
            runIdleScheduler();
        } else if (currentCompanionMode === 2 && beibeiGrid.length > 0) {
            // 🪞 模式二：跟臉！這裡先放正中央那張避免空白，
            //            真正的即時換圖交給 onResults 每幀去做
            if (!aiImg.src) aiImg.src = beibeiGrid[Math.floor(beibeiGrid.length / 2)];
            setDebugLabel("跟臉中（VR 臉部同步）");
        } else {
            // 保底：沒有網格資料時就乖乖發呆
            setAiFrame(aiImg.dataset.idleB64);
            setDebugLabel("idle（無網格資料）");
        }

        aiImg.style.display = 'block';
        aiImgFade.style.display = 'block';
        debugLabel.style.display = 'block';

    } else {
// 🌟 1. 隱藏 AI 圖片，顯示原生 Live2D 畫布
        const canvas = document.getElementById('live2d_canvas');
        if (canvas) canvas.style.display = 'block';
        
        let aiImg = document.getElementById('ai_beibei_img');
        if (aiImg) aiImg.style.display = 'none';
        let aiImgFadeEl = document.getElementById('ai_beibei_img_fade');
        if (aiImgFadeEl) aiImgFadeEl.style.display = 'none';
        const debugLabelEl = document.getElementById('beibei_debug_label');
        if (debugLabelEl) debugLabelEl.style.display = 'none';
        
        // 🌟 2. 動態調整 Live2D 模型大小與位置
        if (beibeiModel) {
            // 讀取 Python 傳來的參數，如果沒傳就用預設值 1.0 和 0
            let scale = args.model_scale !== undefined ? args.model_scale : 1.0;
            let offsetX = args.model_x !== undefined ? args.model_x : 0;
            let offsetY = args.model_y !== undefined ? args.model_y : 0;

            // 縮放：Live2D 基礎大小為 0.2，乘上使用者的縮放倍率
            beibeiModel.scale.set(0.2 * scale); 
            
            // X軸：螢幕正中間，加上使用者的左右平移
            beibeiModel.x = (app.screen.width / 2) + offsetX;
            
            // Y軸：螢幕正中間偏下，減去使用者的上下平移 (因為 Y 軸越往下數值越大)
            beibeiModel.y = (app.screen.height / 2 + 250) - offsetY;
        }

        // 🌟 3. Live2D 原生模型的 VR 同步切換
        if (beibeiModel && typeof args.companion_mode !== 'undefined') {
            if (args.companion_mode === 2) {
                // 模式二：VR 同步 (允許跟著使用者的臉轉頭)
                beibeiModel.internalModel.coreModel.setParameterValueById('ParamAngleX', currentFaceAngleX);
                beibeiModel.internalModel.coreModel.setParameterValueById('ParamAngleY', currentFaceAngleY);
            } else {
                // 模式一：自主模式 (把頭部轉動歸零，不跟著鏡頭轉)
                beibeiModel.internalModel.coreModel.setParameterValueById('ParamAngleX', 0);
                beibeiModel.internalModel.coreModel.setParameterValueById('ParamAngleY', 0);
            }
        }
    }
});

// 2. 🌟 保險起見，我們在外面補上這句，確保 Streamlit 確實收到「我活著」的訊號
