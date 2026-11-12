#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-2 防漂綠預警模型 —— 合成資料實驗（可重現）
====================================================================
目的：在沒有真實資料的 PoC 階段，用「自行設計的合成資料」驗證
      「孤立森林（Isolation Forest）能否找出合法但可疑的碳權歸屬宣告」。

流程：
  1. 產生合成資料：80 家供應商 × 各 12 次歸屬宣告 = 960 筆事件、500 個客戶
  2. 注入四種異常（T1–T4，各 14 筆，共 56 筆 ≈ 5.8%，互不重疊）
  3. 計算 7 維特徵
  4. 比較四種作法：孤立森林、人工門檻規則、兩者混合、
     以及「各供應商加鹽雜湊」情境（看不到跨供應商資訊）
  5. 以 20 組隨機種子（0–19）重複，取平均

執行：  python3 ai2_experiment.py            （約 1 分鐘）
輸出：  終端機列印結果表；同目錄產生 ai2_result.png

注意：所有資料皆為合成資料，數字只說明「方法與流程可運作」，
      不代表在真實資料上的效能。
需求：  numpy、scikit-learn、matplotlib
"""
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.metrics import roc_auc_score, average_precision_score, roc_curve

# ------------------------------------------------------------------
# 實驗參數（集中在這裡，方便調整與說明）
# ------------------------------------------------------------------
N_SUPPLIERS = 80          # 供應商數
N_EVENTS_PER = 12         # 每家供應商的宣告次數（約每月一次）
N_CUSTOMERS = 500         # 客戶母體大小
N_PER_TYPE = 14           # 每種異常注入筆數（4 種 × 14 = 56 筆 ≈ 5.8%）
ALERT_BUDGET = 0.06       # 警示名額：稽核人力每期只看風險最高的 6% 事件
N_TREES = 300             # 孤立森林的樹數
SEEDS = range(20)         # 20 組隨機種子
WINDOW_DAYS = 30          # 跨供應商比對的時間窗（±30 天）
TONNAGE_TOL = 0.05        # 「噸數吻合」的容許誤差（±5%）
TYPES = ['T1 時間差異常', 'T2 跨供應商重複宣告', 'T3 歸屬比例突變', 'T4 碎片化拆分']


# ------------------------------------------------------------------
# 1. 產生合成資料
# ------------------------------------------------------------------
def make_data(seed):
    """回傳事件清單 events（list of dict）與標籤 label（0=正常，1–4=異常類型）。"""
    rng = np.random.default_rng(seed)
    events = []
    # 每家供應商有自己的「平均歸屬比例」μs ~ N(0.75, 0.08)
    mu_s = np.clip(rng.normal(0.75, 0.08, N_SUPPLIERS), 0.45, 0.90)
    for s in range(N_SUPPLIERS):
        for k in range(N_EVENTS_PER):
            retire_day = k * 30 + rng.uniform(0, 30)             # 註銷日（第幾天）
            retired = rng.lognormal(np.log(500), 0.5)            # 註銷量（噸），中位數 500
            ratio = np.clip(rng.normal(mu_s[s], 0.05), 0.2, 0.94)  # 歸屬量 / 註銷量
            gap = rng.lognormal(np.log(21), 0.6)                 # 註銷到歸屬的落差天數，中位數 21 天
            n_cust = int(rng.integers(2, 13))                    # 客戶數 2–12
            custs = rng.choice(N_CUSTOMERS, n_cust, replace=False)
            share = rng.dirichlet(np.ones(n_cust) * 2)           # 各客戶分到的比例
            tons = retired * ratio * share
            events.append(dict(supplier=s, retire_day=retire_day, retired=retired,
                               gap=gap, custs=list(custs), tons=list(tons)))
    n = len(events)
    label = np.zeros(n, dtype=int)

    # ---------------- 2. 注入四種異常（互不重疊） ----------------
    chosen = rng.choice(n, 4 * N_PER_TYPE, replace=False)
    groups = chosen.reshape(4, N_PER_TYPE)

    # T1 時間差異常：註銷後幾乎立刻宣告，或沉睡很久才補宣告
    for i in groups[0]:
        events[i]['gap'] = rng.uniform(0, 0.9) if rng.random() < 0.5 else rng.uniform(250, 400)
        label[i] = 1

    # T2 跨供應商重複宣告：複製「另一家供應商」±30 天內的 2–4 筆客戶歸屬，噸數誤差 ±1%
    for i in groups[1]:
        e = events[i]
        day_i = e['retire_day'] + e['gap']
        cands = [j for j, o in enumerate(events)
                 if o['supplier'] != e['supplier'] and label[j] == 0
                 and abs(o['retire_day'] + o['gap'] - day_i) <= WINDOW_DAYS and len(o['custs']) >= 2]
        if not cands:
            continue
        o = events[int(rng.choice(cands))]
        m = int(min(rng.integers(2, 5), len(o['custs'])))
        pick = rng.choice(len(o['custs']), m, replace=False)
        total = sum(e['tons'])                             # 記下原本的歸屬總量
        keep = max(len(e['custs']) - m, 1)                 # 用複製來的客戶「取代」自己的部分客戶
        e['custs'], e['tons'] = e['custs'][:keep], e['tons'][:keep]
        copied = [o['tons'][p] * rng.uniform(0.99, 1.01) for p in pick]
        rest = max(total - sum(copied), 0.05 * total)      # 其餘客戶等比例縮放，讓總量（歸屬比例）大致不變
        scale = rest / sum(e['tons'])
        e['tons'] = [t * scale for t in e['tons']] + copied
        e['custs'] = e['custs'] + [o['custs'][p] for p in pick]
        label[i] = 2

    # T3 歸屬比例突變：把註銷量幾乎全數對外宣告（與自身歷史不符）
    for i in groups[2]:
        e = events[i]
        new_ratio = rng.uniform(0.96, 1.0)
        scale = new_ratio * e['retired'] / sum(e['tons'])
        e['tons'] = [t * scale for t in e['tons']]
        label[i] = 3

    # T4 碎片化拆分：拆成大量小額客戶，規避單筆審查
    for i in groups[3]:
        e = events[i]
        total = sum(e['tons'])
        n_cust = int(rng.integers(25, 46))
        e['custs'] = list(rng.choice(N_CUSTOMERS, n_cust, replace=False))
        e['tons'] = list(total * rng.dirichlet(np.ones(n_cust) * 5))
        label[i] = 4

    return events, label


# ------------------------------------------------------------------
# 3. 特徵工程（7 維）
# ------------------------------------------------------------------
def make_features(events, salted=False):
    """
    特徵：
      0 gap        歸屬落差天數
      1 ratio      歸屬比例（歸屬量 / 註銷量）
      2 dev        與該供應商其他事件平均比例的偏離（絕對值）
      3 n_cust     客戶數
      4 med_tons   單客戶噸數中位數
      5 overlap    跨供應商客戶重複度：自己的客戶中，有幾位在 ±30 天內也出現在別家供應商的宣告
      6 match      噸數吻合度：上述重複客戶中，噸數相差 ±5% 以內的筆數
    salted=True 模擬「各供應商加鹽雜湊」：同一客戶在不同供應商處的 ID 不同，
    平台無法做跨供應商比對，因此最後兩維為 0。
    """
    n = len(events)
    X = np.zeros((n, 7))
    ratio = np.array([sum(e['tons']) / e['retired'] for e in events])
    sup = np.array([e['supplier'] for e in events])
    day = np.array([e['retire_day'] + e['gap'] for e in events])
    for i, e in enumerate(events):
        same = (sup == e['supplier'])
        same[i] = False                                   # 排除自己，算「歷史平均」
        X[i, 0] = e['gap']
        X[i, 1] = ratio[i]
        X[i, 2] = abs(ratio[i] - ratio[same].mean())
        X[i, 3] = len(e['custs'])
        X[i, 4] = np.median(e['tons'])
        if not salted:
            near = np.where((sup != e['supplier']) & (np.abs(day - day[i]) <= WINDOW_DAYS))[0]
            other = {}                                    # 客戶 -> 別家供應商宣告的噸數清單
            for j in near:
                for c, t in zip(events[j]['custs'], events[j]['tons']):
                    other.setdefault(c, []).append(t)
            for c, t in zip(e['custs'], e['tons']):
                if c in other:
                    X[i, 5] += 1
                    if any(abs(t - u) / u <= TONNAGE_TOL for u in other[c]):
                        X[i, 6] += 1
    return X


# ------------------------------------------------------------------
# 4. 四種作法
# ------------------------------------------------------------------
def iforest_score(X, seed):
    """孤立森林：無監督，不使用標籤；分數愈高愈可疑。"""
    Xt = X.copy()
    Xt[:, 0] = np.log1p(Xt[:, 0])      # 落差天數右偏，取對數讓「太短」與「太長」都容易被切開
    Xt[:, 4] = np.log1p(Xt[:, 4])
    model = IsolationForest(n_estimators=N_TREES, random_state=seed).fit(Xt)
    return -model.score_samples(Xt)


def rule_flags(X):
    """人工門檻規則基準：任一條件成立即警示。"""
    return ((X[:, 0] < 1) | (X[:, 0] > 240) |      # 落差 <1 天或 >240 天
            (X[:, 1] > 0.95) |                     # 歸屬比例 >0.95
            (X[:, 3] > 20) |                       # 客戶數 >20
            (X[:, 6] > 0))                         # 有任何噸數吻合的跨供應商重複


def top_budget(score, budget=ALERT_BUDGET):
    """取分數最高的 budget 比例作為警示名單。"""
    k = int(round(len(score) * budget))
    flag = np.zeros(len(score), dtype=bool)
    flag[np.argsort(-score)[:k]] = True
    return flag


def evaluate(score, flag, y, label):
    """回傳 ROC-AUC、PR-AUC、Precision、Recall、警示比例、各類型召回率。"""
    tp = (flag & (y == 1)).sum()
    per_type = [(flag & (label == t)).sum() / max((label == t).sum(), 1) for t in (1, 2, 3, 4)]
    return dict(roc=roc_auc_score(y, score), pr=average_precision_score(y, score),
                prec=tp / max(flag.sum(), 1), rec=tp / y.sum(),
                alert=flag.mean(), per_type=per_type)


# ------------------------------------------------------------------
# 5. 主程式：20 組種子平均
# ------------------------------------------------------------------
def main():
    names = ['孤立森林（平台假名 ID）', '人工門檻規則', '孤立森林 + 規則（混合）', '孤立森林（各供應商加鹽雜湊）']
    results = {k: [] for k in names}
    roc_points = {}
    for seed in SEEDS:
        events, label = make_data(seed)
        y = (label > 0).astype(int)
        X = make_features(events)                 # 平台假名 ID：可跨供應商比對
        Xs = make_features(events, salted=True)   # 加鹽雜湊：無法跨供應商比對

        s_if = iforest_score(X, seed)
        f_rule = rule_flags(X)
        s_rule = f_rule.astype(float)             # 規則只有「有／無」，以 0/1 當分數
        # 混合：規則命中者優先，再依孤立森林分數排序（分數正規化到 0–1 後加上規則的 1 分）
        s_mix = (s_if - s_if.min()) / (s_if.max() - s_if.min()) + f_rule
        s_salt = iforest_score(Xs, seed)

        results[names[0]].append(evaluate(s_if, top_budget(s_if), y, label))
        results[names[1]].append(evaluate(s_rule, f_rule, y, label))        # 規則的警示量不受 6% 限制
        results[names[2]].append(evaluate(s_mix, top_budget(s_mix), y, label))
        results[names[3]].append(evaluate(s_salt, top_budget(s_salt), y, label))
        if seed == 0:                             # 圖中的 ROC 曲線以種子 0 為例
            roc_points = {names[0]: roc_curve(y, s_if), names[2]: roc_curve(y, s_mix), names[3]: roc_curve(y, s_salt)}

    print(f'資料：{N_SUPPLIERS} 家供應商 × {N_EVENTS_PER} 次 = {N_SUPPLIERS * N_EVENTS_PER} 筆事件；'
          f'異常 {4 * N_PER_TYPE} 筆（{4 * N_PER_TYPE / (N_SUPPLIERS * N_EVENTS_PER):.1%}）；{len(SEEDS)} 組種子平均\n')
    print(f'{"方法":<26}{"ROC-AUC":>9}{"PR-AUC":>9}{"Precision":>11}{"Recall":>9}{"警示量":>8}')
    mean = {}
    for k in names:
        r = results[k]
        m = {key: np.mean([x[key] for x in r]) for key in ('roc', 'pr', 'prec', 'rec', 'alert')}
        m['per_type'] = np.mean([x['per_type'] for x in r], axis=0)
        mean[k] = m
        print(f'{k:<26}{m["roc"]:>9.3f}{m["pr"]:>9.3f}{m["prec"]:>11.3f}{m["rec"]:>9.3f}{m["alert"]:>8.1%}')
    print('\n各異常類型的召回率（6% 警示名額；規則為其全部警示）')
    print(f'{"方法":<26}' + ''.join(f'{t[:2]:>8}' for t in TYPES))
    for k in names:
        print(f'{k:<26}' + ''.join(f'{v:>8.2f}' for v in mean[k]['per_type']))

    # ---------------- 繪圖 ----------------
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        for f in ('DFKai-SB', 'Microsoft JhengHei', 'PingFang TC', 'Noto Sans CJK TC', 'Noto Sans CJK JP'):
            if any(f == x.name for x in font_manager.fontManager.ttflist):
                plt.rcParams['font.family'] = f
                break
        plt.rcParams['axes.unicode_minus'] = False
        fig, ax = plt.subplots(1, 2, figsize=(11, 4.4))
        colors = {names[0]: '#1E5A46', names[2]: '#2F5DA8', names[3]: '#C98A00'}
        for k, (fpr, tpr, _) in roc_points.items():
            ax[0].plot(fpr, tpr, color=colors[k], label=f'{k}（平均 AUC={mean[k]["roc"]:.3f}）')
        ax[0].plot([0, 1], [0, 1], '--', color='#999999', lw=1)
        ax[0].set_xlabel('False Positive Rate'); ax[0].set_ylabel('True Positive Rate')
        ax[0].set_title('ROC 曲線（第 1 組種子）'); ax[0].legend(fontsize=8, loc='lower right')
        w = 0.27
        for n_, k in enumerate((names[0], names[3], names[2])):
            ax[1].bar(np.arange(4) + (n_ - 1) * w, mean[k]['per_type'], w, color=colors[k], label=k)
        ax[1].set_xticks(np.arange(4)); ax[1].set_xticklabels([t.replace(' ', '\n') for t in TYPES], fontsize=8)
        ax[1].set_ylabel('Recall（6% 警示名額）'); ax[1].set_title(f'各異常情境召回率（{len(SEEDS)} 組種子平均）')
        ax[1].legend(fontsize=8)
        fig.tight_layout(); fig.savefig('ai2_result.png', dpi=200)
        print('\n圖已輸出：ai2_result.png')
    except Exception as ex:       # 沒有 matplotlib 也不影響數字結果
        print('\n（未輸出圖：', ex, '）')


if __name__ == '__main__':
    main()
