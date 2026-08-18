// Streamlit Component 通訊庫（符合官方 postMessage 協定版）
// 關鍵：對外送出的訊息一律帶 isStreamlitMessage:true，新版 Streamlit 才會接受；
//       setFrameHeight 用 height 欄位、setComponentValue 帶 dataType，避免被忽略。
(function () {
  function send(type, data) {
    var msg = { isStreamlitMessage: true, type: type };
    if (data) { for (var k in data) msg[k] = data[k]; }
    window.parent.postMessage(msg, "*");
  }
  var Streamlit = {
    setComponentValue: function (value) {
      send("streamlit:setComponentValue", { value: value, dataType: "json" });
    },
    setFrameHeight: function (height) {
      send("streamlit:setFrameHeight", { height: height });
    },
    setComponentReady: function () {
      send("streamlit:componentReady", { apiVersion: 1 });
    },
    onRender: function (callback) {
      window.addEventListener("message", function (event) {
        if (event.data && event.data.type === "streamlit:render") {
          callback(event.data.args || {});
        }
      });
      // 告知 Streamlit：元件已就緒（送出後 Streamlit 才會回傳 render 事件）
      send("streamlit:componentReady", { apiVersion: 1 });
    }
  };
  window.Streamlit = Streamlit;
})();
