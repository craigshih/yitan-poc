// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC1155} from "@openzeppelin/contracts@5.0.2/token/ERC1155/ERC1155.sol";
import {ERC1155Holder} from "@openzeppelin/contracts@5.0.2/token/ERC1155/utils/ERC1155Holder.sol";
import {EIP712} from "@openzeppelin/contracts@5.0.2/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts@5.0.2/utils/cryptography/ECDSA.sol";

/// @title 一碳究竟 CarbonRegistry（PoC）
/// @notice 碳權官方紀錄的鏈上存證：七個狀態、五條規則。代幣是「尚未註銷額度」的紀錄，
///         全數由本合約託管、不可自由轉讓；註銷即銷毀，只留不可轉讓的歸屬紀錄。
/// @dev 任何人都可以當 relayer 送交易，但每筆操作都必須帶權責機關（環境部／碳交所）的 EIP-712 簽章。
contract CarbonRegistry is ERC1155, ERC1155Holder, EIP712 {
    struct Auth { uint256 nonce; uint256 deadline; bytes sig; }
    struct Project { bool exists; bool early; bytes32 pddHash; string vvb; string license; }
    struct Batch { bytes32 project; uint256 tons; uint256 serialFrom; uint256 serialTo; bool listed; string doc; }
    struct Retirement { uint256 batch; bytes32 holder; uint256 tons; uint256 attributed; uint8 purpose; bool published; }

    uint8 public constant PURPOSE_OFFSET = 0;      // 自願抵銷／碳中和宣告：可對外歸屬
    uint8 public constant PURPOSE_CARBON_FEE = 1;  // 碳費扣除：不得對外歸屬

    // shortcut: 所有操作共用一個 EIP-712 型別（kind + 參數雜湊），錢包畫面看不到逐欄位內容；
    // 正式版改為每個操作各自的具名型別。
    bytes32 private constant OP_TYPEHASH = keccak256("Op(bytes32 kind,bytes32 dataHash,uint256 nonce,uint256 deadline)");

    address public immutable moenv; // 環境部簽章金鑰
    address public immutable tcx;   // 碳交所簽章金鑰
    mapping(address => uint256) public nonces;

    mapping(bytes32 => Project) public projects;
    mapping(uint256 => Batch) public batches;
    uint256[] public batchIds;
    mapping(uint256 => mapping(bytes32 => uint256)) public held; // 已移轉、尚未註銷：batch => 持有者假名 => 噸數
    mapping(bytes32 => Retirement) public retirements;

    event ProjectRegistered(bytes32 indexed projectId, bool early, bytes32 pddHash, string vvb, string license);
    event Issued(uint256 indexed batchId, bytes32 indexed projectId, uint256 tons, uint256 serialFrom, uint256 serialTo, string doc);
    event Listed(uint256 indexed batchId);
    event Transferred(uint256 indexed batchId, bytes32 indexed buyer, uint256 tons, bool negotiated);
    event Retired(bytes32 indexed retId, uint256 indexed batchId, bytes32 indexed holder, uint256 tons, uint8 purpose, string note);
    event UsagePublished(bytes32 indexed retId);
    event Attributed(bytes32 indexed retId, uint256 tons, bytes32 commitment, uint256 totalAttributed);

    error BadSignature();
    error Expired();
    error AlreadyExists();
    error NotFound();
    error SerialAlreadyRegistered();          // 規則①
    error EarlyCreditNegotiationOnly();       // 規則②・交易辦法第 10 條
    error NotEarlyCredit();
    error NotListed();
    error AlreadyTransferredOnce();           // 規則③・第 26 條
    error InsufficientBalance();              // 規則④：已銷毀的額度不可再用
    error UsageNotPublished();                // 規則⑤・第 27 條
    error PurposeNotAttributable();           // 規則⑤：碳費扣除不可再宣告
    error ExceedsRetired(uint256 remaining);  // 規則⑤：累計歸屬不得超過註銷量
    error NonTransferable();

    constructor(address moenv_, address tcx_) ERC1155("") EIP712("CarbonRegistry", "1") {
        moenv = moenv_;
        tcx = tcx_;
    }

    // ---------- 七個狀態 ----------

    function registerProject(bytes32 projectId, bool early, bytes32 pddHash, string calldata vvb, string calldata license, Auth calldata a) external {
        _auth(moenv, keccak256("registerProject"), abi.encode(projectId, early, pddHash, vvb, license), a);
        if (projects[projectId].exists) revert AlreadyExists();
        projects[projectId] = Project(true, early, pddHash, vvb, license);
        emit ProjectRegistered(projectId, early, pddHash, vvb, license);
    }

    function issue(uint256 batchId, bytes32 projectId, uint256 tons, uint256 serialFrom, uint256 serialTo, string calldata doc, Auth calldata a) external {
        _auth(moenv, keccak256("issue"), abi.encode(batchId, projectId, tons, serialFrom, serialTo, doc), a);
        if (!projects[projectId].exists || tons == 0) revert NotFound();
        if (batches[batchId].tons != 0) revert AlreadyExists();
        // shortcut: 線性掃描既有批次檢查序號重疊；批次數量大時改用區間樹或以登記簿序號為 key
        for (uint256 i = 0; i < batchIds.length; i++) {
            Batch storage b = batches[batchIds[i]];
            if (serialFrom <= b.serialTo && serialTo >= b.serialFrom) revert SerialAlreadyRegistered();
        }
        batches[batchId] = Batch(projectId, tons, serialFrom, serialTo, false, doc);
        batchIds.push(batchId);
        _mint(address(this), _open(batchId), tons, "");
        emit Issued(batchId, projectId, tons, serialFrom, serialTo, doc);
    }

    function list(uint256 batchId, Auth calldata a) external {
        _auth(tcx, keccak256("list"), abi.encode(batchId), a);
        Batch storage b = _batch(batchId);
        if (projects[b.project].early) revert EarlyCreditNegotiationOnly();
        if (b.listed) revert SerialAlreadyRegistered();
        b.listed = true;
        emit Listed(batchId);
    }

    /// @param seller 0 代表由發行池成交；非 0 代表「已移轉過的持有者想再賣」，一律拒絕
    function trade(uint256 batchId, bytes32 seller, bytes32 buyer, uint256 tons, Auth calldata a) external {
        _auth(tcx, keccak256("trade"), abi.encode(batchId, seller, buyer, tons), a);
        Batch storage b = _batch(batchId);
        if (seller != bytes32(0)) revert AlreadyTransferredOnce();
        if (!b.listed) revert NotListed();
        _move(batchId, buyer, tons, false);
    }

    /// @notice 先期額度的協議交易：不經碳交所，由環境部核准並簽章
    function negotiate(uint256 batchId, bytes32 buyer, uint256 tons, Auth calldata a) external {
        _auth(moenv, keccak256("negotiate"), abi.encode(batchId, buyer, tons), a);
        if (!projects[_batch(batchId).project].early) revert NotEarlyCredit();
        _move(batchId, buyer, tons, true);
    }

    function retire(bytes32 retId, uint256 batchId, bytes32 holder, uint256 tons, uint8 purpose, string calldata note, Auth calldata a) external {
        _auth(moenv, keccak256("retire"), abi.encode(retId, batchId, holder, tons, purpose, note), a);
        _batch(batchId);
        if (retirements[retId].tons != 0) revert AlreadyExists();
        if (tons == 0 || tons > held[batchId][holder]) revert InsufficientBalance();
        held[batchId][holder] -= tons;
        _burn(address(this), _moved(batchId), tons);
        retirements[retId] = Retirement(batchId, holder, tons, 0, purpose, false);
        emit Retired(retId, batchId, holder, tons, purpose, note);
    }

    function publishUsage(bytes32 retId, Auth calldata a) external {
        _auth(moenv, keccak256("publishUsage"), abi.encode(retId), a);
        Retirement storage r = retirements[retId];
        if (r.tons == 0) revert NotFound();
        r.published = true;
        emit UsagePublished(retId);
    }

    /// @param commitment 鏈下明細（客戶、噸數、鹽）的雜湊承諾；鏈上不存客戶名單
    function attribute(bytes32 retId, uint256 tons, bytes32 commitment, Auth calldata a) external {
        _auth(tcx, keccak256("attribute"), abi.encode(retId, tons, commitment), a);
        Retirement storage r = retirements[retId];
        if (r.tons == 0) revert NotFound();
        if (!r.published) revert UsageNotPublished();
        if (r.purpose != PURPOSE_OFFSET) revert PurposeNotAttributable();
        if (r.attributed + tons > r.tons) revert ExceedsRetired(r.tons - r.attributed);
        r.attributed += tons;
        emit Attributed(retId, tons, commitment, r.attributed);
    }

    // ---------- 內部 ----------

    function _auth(address role, bytes32 kind, bytes memory data, Auth calldata a) private {
        if (block.timestamp > a.deadline) revert Expired();
        bytes32 digest = _hashTypedDataV4(keccak256(abi.encode(OP_TYPEHASH, kind, keccak256(data), a.nonce, a.deadline)));
        if (ECDSA.recover(digest, a.sig) != role || a.nonce != nonces[role]) revert BadSignature();
        nonces[role]++;
    }

    function _batch(uint256 batchId) private view returns (Batch storage b) {
        b = batches[batchId];
        if (b.tons == 0) revert NotFound();
    }

    function _move(uint256 batchId, bytes32 buyer, uint256 tons, bool negotiated) private {
        if (tons == 0 || tons > balanceOf(address(this), _open(batchId))) revert InsufficientBalance();
        _burn(address(this), _open(batchId), tons);
        _mint(address(this), _moved(batchId), tons, "");
        held[batchId][buyer] += tons;
        emit Transferred(batchId, buyer, tons, negotiated);
    }

    function _open(uint256 batchId) private pure returns (uint256) { return batchId << 1; }        // 可移轉
    function _moved(uint256 batchId) private pure returns (uint256) { return (batchId << 1) | 1; } // 已移轉，只能註銷

    /// @dev 只允許鑄造與銷毀；代幣不是可交易權利
    function _update(address from, address to, uint256[] memory ids, uint256[] memory values) internal override {
        if (from != address(0) && to != address(0)) revert NonTransferable();
        super._update(from, to, ids, values);
    }

    function supportsInterface(bytes4 id) public view override(ERC1155, ERC1155Holder) returns (bool) {
        return super.supportsInterface(id);
    }
}
