// Streamlit Component 通訊庫
// 作用：讓瀏覽器裡的 JavaScript 能把數據傳回給 Python 的 Streamlit
(function() {
  var Streamlit = {
    setComponentValue: function(value) {
      var data = {
        type: "streamlit:setComponentValue",
        value: value,
      };
      window.parent.postMessage(data, "*");
    },
    setFrameHeight: function(height) {
      var data = {
        type: "streamlit:setFrameHeight",
        value: height,
      };
      window.parent.postMessage(data, "*");
    },
    onRender: function(callback) {
      var onMessageEvent = function(event) {
        if (event.data.type === "streamlit:render") {
          callback(event.data.args);
        }
      };
      window.addEventListener("message", onMessageEvent);
      
      // 告知 Streamlit 組件已準備就緒
      var data = {
        type: "streamlit:componentReady",
        apiVersion: 1,
      };
      window.parent.postMessage(data, "*");
    }
  };
  window.Streamlit = Streamlit;
})();