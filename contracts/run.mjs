// 編譯 → 部署 → 跑第一幕 → 驗證七個失敗案例都被合約拒絕。
//   本機測試：  node run.mjs
//   Sepolia：   RPC_URL=... PRIVATE_KEY=0x... node run.mjs     （PRIVATE_KEY 請用只放測試幣的錢包）
import fs from 'node:fs';
import assert from 'node:assert/strict';
import solc from 'solc';
import { ethers } from 'ethers';

// ---- 編譯 ----
const OZ = '@openzeppelin/contracts@5.0.2/';
const input = {
  language: 'Solidity',
  sources: { 'CarbonRegistry.sol': { content: fs.readFileSync(new URL('CarbonRegistry.sol', import.meta.url), 'utf8') } },
  settings: { optimizer: { enabled: true, runs: 200 }, evmVersion: 'paris', outputSelection: { '*': { '*': ['abi', 'evm.bytecode.object'] } } },
};
const out = JSON.parse(solc.compile(JSON.stringify(input), {
  import: p => p.startsWith(OZ)
    ? { contents: fs.readFileSync(new URL('node_modules/@openzeppelin/contracts/' + p.slice(OZ.length), import.meta.url), 'utf8') }
    : { error: 'not found: ' + p },
}));
const errs = (out.errors || []).filter(e => e.severity === 'error');
if (errs.length) { console.error(errs.map(e => e.formattedMessage).join('\n')); process.exit(1); }
const { abi, evm } = out.contracts['CarbonRegistry.sol'].CarbonRegistry;

// ---- 連線 ----
const live = !!process.env.RPC_URL;
let relayer;
if (live) {
  relayer = new ethers.Wallet(process.env.PRIVATE_KEY, new ethers.JsonRpcProvider(process.env.RPC_URL));
} else {
  const { default: ganache } = await import('ganache');
  relayer = await new ethers.BrowserProvider(ganache.provider({ logging: { quiet: true } })).getSigner(0);
}
const chainId = (await relayer.provider.getNetwork()).chainId;
const scan = chainId === 11155111n ? 'https://sepolia.etherscan.io/' : '';
// 機關金鑰只負責簽章、不需要 ETH；PoC 每次執行隨機產生
const moenv = ethers.Wallet.createRandom(), tcx = ethers.Wallet.createRandom(), attacker = ethers.Wallet.createRandom();

const c = await (await new ethers.ContractFactory(abi, evm.bytecode.object, relayer).deploy(moenv.address, tcx.address)).waitForDeployment();
const address = await c.getAddress();
console.log(`合約已部署：${scan ? scan + 'address/' : ''}${address}（chainId ${chainId}）`);

// ---- 簽章與送出 ----
const OPS = { // 操作 → [法定簽章機關, 參數型別]
  registerProject: [moenv, ['bytes32', 'bool', 'bytes32', 'string', 'string']],
  issue: [moenv, ['uint256', 'bytes32', 'uint256', 'uint256', 'uint256', 'string']],
  list: [tcx, ['uint256']],
  trade: [tcx, ['uint256', 'bytes32', 'bytes32', 'uint256']],
  negotiate: [moenv, ['uint256', 'bytes32', 'uint256']],
  retire: [moenv, ['bytes32', 'uint256', 'bytes32', 'uint256', 'uint8', 'string']],
  publishUsage: [moenv, ['bytes32']],
  attribute: [tcx, ['bytes32', 'uint256', 'bytes32']],
};
const id = s => ethers.id(s);
const domain = { name: 'CarbonRegistry', version: '1', chainId, verifyingContract: address };
const types = { Op: [{ name: 'kind', type: 'bytes32' }, { name: 'dataHash', type: 'bytes32' }, { name: 'nonce', type: 'uint256' }, { name: 'deadline', type: 'uint256' }] };
async function auth(op, args, signer) {
  const [role, t] = OPS[op];
  const nonce = await c.nonces(role.address), deadline = Math.floor(Date.now() / 1000) + 3600;
  const dataHash = ethers.keccak256(ethers.AbiCoder.defaultAbiCoder().encode(t, args));
  return { nonce, deadline, sig: await (signer || role).signTypedData(domain, types, { kind: id(op), dataHash, nonce, deadline }) };
}
const txs = [];
async function ok(label, op, ...args) {
  const r = await (await c[op](...args, await auth(op, args))).wait();
  txs.push({ label, op, hash: r.hash });
  console.log(`✓ ${label}　${scan ? scan + 'tx/' : ''}${r.hash}`);
}
const rejected = [];
async function mustRevert(label, expected, op, args, signer) {
  let name;
  try { await c[op].staticCall(...args, await auth(op, args, signer)); } catch (e) { name = e.revert?.name; }
  assert.equal(name, expected, `${label} 應被 ${expected} 拒絕，實際：${name}`);
  rejected.push({ label, error: name });
  console.log(`✕ ${label}　合約拒絕：${name}`);
}

// ---- 第一幕 ----
const V = 1n, E = 2n, PV = id('P-VOL-01'), PE = id('P-EARLY-01'), GL = id('綠光電子'), R1 = id('RET-001');
const commit = code => id(`RET-001|${code}`);
await ok('專案註冊', 'registerProject', PV, false, id('計畫書'), '綠證查驗股份有限公司（虛構）', 'VVB-DEMO-007');
await ok('核發 1,000 噸', 'issue', V, PV, 1000n, 1n, 1000n, '環部氣字第 DEMO-1150301 號（模擬）');
await ok('上架', 'list', V);
await ok('綠光電子買入 100 噸', 'trade', V, ethers.ZeroHash, GL, 100n);
await ok('註銷 60 噸（產品碳中和宣告）', 'retire', R1, V, GL, 60n, 0, '2026 年產品碳中和宣告');
await ok('公告註銷用途', 'publishUsage', R1);
await ok('歸屬 B 公司 40 噸', 'attribute', R1, 40n, commit('B公司-40-salt'));
await ok('歸屬 C 公司 20 噸', 'attribute', R1, 20n, commit('C公司-20-salt'));

// ---- 七個失敗案例（先備好需要的狀態）----
await ok('先期專案註冊', 'registerProject', PE, true, id('先期計畫書'), '', '');
await ok('核發先期額度 500 噸', 'issue', E, PE, 500n, 1001n, 1500n, '環部氣字第 DEMO-1150412 號（模擬）');
await ok('註銷 10 噸（尚未公告）', 'retire', id('RET-002'), V, GL, 10n, 0, '產品碳中和宣告');
await ok('註銷 10 噸（碳費扣除）', 'retire', id('RET-003'), V, GL, 10n, 1, '碳費扣除');
await ok('公告用途：碳費扣除', 'publishUsage', id('RET-003'));

await mustRevert('重複上架', 'SerialAlreadyRegistered', 'list', [V]);
await mustRevert('超額歸屬', 'ExceedsRetired', 'attribute', [R1, 10n, commit('D公司-10-salt')]);
await mustRevert('偽造來源', 'BadSignature', 'trade', [V, ethers.ZeroHash, id('不明買方'), 50n], attacker);
await mustRevert('先期額度上架', 'EarlyCreditNegotiationOnly', 'list', [E]);
await mustRevert('再次轉售', 'AlreadyTransferredOnce', 'trade', [V, GL, id('D公司'), 20n]);
await mustRevert('公告前歸屬', 'UsageNotPublished', 'attribute', [id('RET-002'), 10n, commit('E公司-10-salt')]);
await mustRevert('一噸兩用', 'PurposeNotAttributable', 'attribute', [id('RET-003'), 10n, commit('B公司-10-salt')]);

// ---- 最終狀態檢查 ----
const r = await c.retirements(R1);
assert.equal(r.attributed, 60n);                              // 失敗案例沒有改變歸屬量
assert.equal(await c.balanceOf(address, V << 1n), 900n);      // 可移轉
assert.equal(await c.balanceOf(address, (V << 1n) | 1n), 20n); // 已移轉未註銷
assert.equal(await c.held(V, GL), 20n);
fs.writeFileSync(new URL(`deployment-${chainId}.json`, import.meta.url), JSON.stringify({ chainId: Number(chainId), address, moenv: moenv.address, tcx: tcx.address, txs, rejected }, null, 2));
console.log(`\n全部通過：13 筆交易上鏈、7 個違規被合約拒絕。紀錄已寫入 deployment-${chainId}.json`);
