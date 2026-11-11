# CarbonRegistry 合約

`CarbonRegistry.sol` 把互動原型的五條規則寫成 Solidity：ERC-1155 批次代幣（全數由合約託管、不可轉讓）、
EIP-712 機關簽章、註銷即銷毀、歸屬只留雜湊承諾。`run.mjs` 一支腳本同時負責編譯、部署、跑第一幕、
驗證七個失敗案例。

## 本機測試（不花錢、不需錢包）

```bash
npm install
```

```bash
node run.mjs
```

看到「全部通過：13 筆交易上鏈、7 個違規被合約拒絕」即成功。
（開頭一段 `uws_darwin_arm64` 的訊息是本機鏈套件的效能提示，可忽略。）

## 部署到 Sepolia

1. 準備一個**只放測試幣**的錢包，匯出私鑰。不要用有真實資產的錢包。
2. 到水龍頭領 Sepolia ETH（整套流程約需 0.02 ETH 以內，視當時 gas 而定）。
3. 準備一個 Sepolia RPC 網址（Alchemy、Infura 或公開節點皆可）。
4. 在這個資料夾執行（把兩個值換成自己的）：

```bash
RPC_URL="https://你的-sepolia-rpc" PRIVATE_KEY="0x你的測試錢包私鑰" node run.mjs
```

約 3–5 分鐘跑完。終端機會印出合約位址與 13 筆交易的 Etherscan 連結，並寫入 `deployment-11155111.json`。

## 部署後要做的事

- 打開合約的 Etherscan 頁面，截「Transactions」與「Events」分頁，就是企劃書缺的「區塊瀏覽器交易雜湊與合約事件畫面」。
- 把企劃書表 23 的「第二階段依時程進行」改成「已部署」，附上合約位址。
- 七個失敗案例是用模擬呼叫驗證的（合約拒絕、不花 gas），所以 Etherscan 上不會有失敗交易；證據是腳本輸出與 `deployment-*.json` 的 `rejected` 欄位。

## 已知限制

- 環境部、碳交所的簽章金鑰每次執行隨機產生，只存在記憶體；重跑會部署一份新合約。
- 持有者以假名雜湊記在鏈上，與企劃書「企業餘額存於中台」的設計不同，PoC 為了讓規則④可在鏈上檢查而簡化。
- 尚未在 Etherscan 驗證原始碼，也未在 Besu 上測試。
- 互動原型（`../index.html`）還沒有接這份合約，查驗頁仍是瀏覽器端模擬。
