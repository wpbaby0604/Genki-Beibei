const videoElement = document.getElementById('input_video');
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
const FACE_SMOOTH = 0.25;   // 0~1：越小越平滑但反應慢，越大越靈敏但較抖

// 🌟 新增：5分鐘主動搭話的專屬變數
let idleTimeout = null;
const IDLE_TIME_LIMIT = 5 * 60 * 1000; // 5 分鐘 = 300,000 毫秒

// 🌟 新增：打瞌睡偵測的專屬變數
let drowsyStartTime = null;
const DROWSY_THRESHOLD = 0.2; // 閉眼判定標準 (跟妳 Python 端一樣)
const DROWSY_TIME_LIMIT = 3000; // 閉眼超過 3 秒 (3000毫秒) 就叫醒

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
                ai.src = ai.dataset.idleB64;
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
            if (!speaking && !drowsy) {   // 說話/打瞌睡動畫優先，其餘時間才跟臉
                const src = beibeiGrid[faceAngleToGridIndex(smoothFaceAngleX, smoothFaceAngleY)];
                if (src && aiImg.src !== src) aiImg.src = src;
            }
        }
    }
    // 核心邏輯：如果低於標準，開始計時
    if (avgEAR < DROWSY_THRESHOLD) {
        if (!drowsyStartTime) {
            drowsyStartTime = Date.now(); // 剛閉上眼睛，記下時間
        } else if (Date.now() - drowsyStartTime > DROWSY_TIME_LIMIT) {
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
});

const camera = new Camera(videoElement, {
    onFrame: async () => { await faceMesh.send({ image: videoElement }); },
    width: 640, height: 480
});
camera.start();

// ==========================================
// 4. Streamlit 通訊（正確做法）
// ==========================================
// ✅ 修正：使用 Streamlit.onRender() 而非手動 addEventListener
//    onRender() 內部會自動送出 streamlit:componentReady 給父視窗
//    這樣 Streamlit 才知道元件已就緒，不會顯示錯誤訊息
Streamlit.onRender(function(args) {
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
            }
        } catch(e) { console.log("解析動畫 JSON 失敗", e); }

 // 🌟 同步目前模式給 onResults 用（新增這行）
        currentCompanionMode = (typeof args.companion_mode !== 'undefined') ? args.companion_mode : 1;

        // 🌟 狀態機切換邏輯
        let isSpeaking = (currentPlayingAudio && !currentPlayingAudio.paused);
        let isDrowsyAlert = (drowsyStartTime && Date.now() - drowsyStartTime > DROWSY_TIME_LIMIT);

        if (isSpeaking) {
            // 講話中：強制動嘴巴
            aiImg.src = aiImg.dataset.talkingB64;
        } else if (isDrowsyAlert) {
            // 打瞌睡：強制播放驚醒動作
            aiImg.src = aiImg.dataset.alertB64;
        } else if (currentCompanionMode === 1) {
            // 🎭 模式一：平常安靜發呆（閉嘴呼吸），約每 25 秒才可能打一次哈欠
            let timeSec = Math.floor(Date.now() / 1000);
            if (timeSec % 25 === 0 && Math.random() < 0.5 && aiImg.dataset.yawnB64) {
                aiImg.src = aiImg.dataset.yawnB64;
            } else {
                aiImg.src = aiImg.dataset.idleB64;
            }
        } else if (currentCompanionMode === 2 && beibeiGrid.length > 0) {
            // 🪞 模式二：跟臉！這裡先放正中央那張避免空白，
            //            真正的即時換圖交給 onResults 每幀去做
            if (!aiImg.src) aiImg.src = beibeiGrid[Math.floor(beibeiGrid.length / 2)];
        } else {
            // 保底：沒有網格資料時就乖乖發呆
            aiImg.src = aiImg.dataset.idleB64;
        }

        aiImg.style.display = 'block';

    } else {
// 🌟 1. 隱藏 AI 圖片，顯示原生 Live2D 畫布
        const canvas = document.getElementById('live2d_canvas');
        if (canvas) canvas.style.display = 'block';
        
        let aiImg = document.getElementById('ai_beibei_img');
        if (aiImg) aiImg.style.display = 'none';
        
        // 🌟 2. 動態調整 Live2D 模型大小與位置
        if (beibeiModel) {
            // 🔧 修正「原生模型往右飄」：重繪當下先把畫布尺寸同步到目前視窗，
            //    避免讀到還沒更新的舊寬度，導致 x 沒對準中心而一次次累積偏移。
            app.resize();

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