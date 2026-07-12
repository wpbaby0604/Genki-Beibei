# baked_avatars — 預烤好的自訂頭像

這裡放「本地用 `bake_avatar.py` 烤好的」自訂頭像 `.json` 檔。

## 怎麼新增一個自訂頭像
1. 在**本地端**（有 THA3 環境的電腦）專案根目錄執行：
   ```
   python bake_avatar.py 你的照片.jpg 頭像名稱
   ```
   會產生 `baked_avatars/頭像名稱.json`
2. 把這個 `.json` 一起 commit、推上 GitHub
3. 雲端模式二「角色外觀設定 → 我的自訂頭像 (THA3 預烤)」的下拉就會出現它

## 注意
- 這個資料夾「要」進 git（跟著上雲），所以**不要**被 .gitignore 擋掉。
- 每個 .json 約 3–6 MB，遠低於 GitHub 單檔 100MB 限制。
- 雲端只是「播放」這些預烤成品，不跑神經網路，所以很穩。
