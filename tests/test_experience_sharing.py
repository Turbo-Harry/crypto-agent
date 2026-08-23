"""
经验共享回归测试（2026-08-23 用户指示"经验共享"——双实例教训互同步,离线隔离临时库）:
  1. share_key 身份哈希: 两库各自自增 id 撞车也能正确镜像
  2. sync_peer_lessons 幂等: 连跑两次不重复插入
  3. peer 镜像 validate 跳过: 教训由产生它的实例验证,防 good/bad 双重计数
  4. 状态传播: origin 侧 validate 晋升 trusted → 同步后镜像侧也 trusted
  5. 镜像混入后本地 add() 的 id 不与镜像撞车(MAX(id)+1)
运行: PYTHONPATH=lib python3 tests/test_experience_sharing.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision.experience_scoring import (ScoredExperience,
                                         sync_peer_lessons,
                                         _share_key_of)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="exp_share_")
    db_a = os.path.join(tmp, "a.db")   # 模拟"模拟盘"实例
    db_b = os.path.join(tmp, "b.db")   # 模拟"实盘"实例
    for p in (db_a, db_b):
        import storage.db as sdb
        sdb.init_db(p)

    # ---- 1. A 侧产生并验证教训 → trusted ----
    bank_a = ScoredExperience(db_a)
    lid = bank_a.add("BTC", "入场", "回踩 30 分钟 EMA 后入场胜率高", "txn_a1",
                     status="candidate")
    check("A 新增教训带 share_key",
          any(l["id"] == lid and l.get("share_key") for l in bank_a.lessons))
    bank_a.validate(lid, 0.01)
    bank_a.validate(lid, 0.02)
    bank_a.validate(lid, 0.03)
    got = [l for l in bank_a.lessons if l["id"] == lid][0]
    check("A 验证 3 次盈利 → trusted", got["status"] == "trusted",
          f"status={got['status']} adoptions={got['adoptions']}")

    # ---- 2. B ← A 同步,镜像 origin=peer ----
    added, updated, r_added = sync_peer_lessons(db_b, db_a)
    check("同步新增 1 条教训", added == 1, f"added={added}")
    bank_b = ScoredExperience(db_b)
    mirror = [l for l in bank_b.lessons if l.get("origin") == "peer"]
    check("B 侧镜像 origin=peer", len(mirror) == 1)
    check("镜像状态=trusted(随 origin 传播)",
          mirror and mirror[0]["status"] == "trusted",
          str(mirror[0].get("status")) if mirror else "无镜像")

    # ---- 3. 幂等: 再同步一次不重复 ----
    added2, updated2, _ = sync_peer_lessons(db_b, db_a)
    check("二次同步幂等(新增0,只更新)", added2 == 0,
          f"added2={added2} updated2={updated2}")
    import storage.db as sdb2
    n = sdb2.q1("SELECT COUNT(*) c FROM lessons WHERE origin='peer'",
                db_path=db_b)["c"]
    check("B 库镜像行数不膨胀", n == 1, f"n={n}")

    # ---- 4. B 侧 validate 跳过 peer 教训 ----
    bank_b = ScoredExperience(db_b)
    mid = mirror[0]["id"]
    r = bank_b.validate(mid, 0.01)
    check("B 验证 peer 教训被跳过(返回 None)", r is None)
    after = [l for l in bank_b.lessons if l["id"] == mid][0]
    check("镜像 adoptions 未被本地+1", after["adoptions"] == 3,
          f"adoptions={after['adoptions']}")

    # ---- 5. 镜像混入后,本地 add() 用 MAX(id)+1 不撞车 ----
    bank_b = ScoredExperience(db_b)
    new_id = bank_b.add("ETH", "入场", "B 侧本地新教训", "txn_b1")
    local_ids = [l["id"] for l in bank_b.lessons if l.get("origin") != "peer"]
    check("镜像混入后 add() 不与镜像 id 撞车",
          new_id not in [l["id"] for l in bank_b.lessons if l.get("origin") == "peer"]
          and len(local_ids) == len(set(local_ids)),
          f"new_id={new_id}")

    # ---- 6. 双向: B 的新教训同步回 A ----
    added3, _, _ = sync_peer_lessons(db_a, db_b)
    check("B 本地教训同步回 A", added3 == 1, f"added3={added3}")
    bank_a2 = ScoredExperience(db_a)
    keys = {l.get("share_key") for l in bank_a2.lessons}
    check("A 库既有 origin 教训又有 peer 镜像",
          len(bank_a2.lessons) == 2 and len(keys) == 2)

    # ---- 7. share_key 确定性(两库独立算出同一身份) ----
    k1 = _share_key_of("BTC", "入场", "回踩 30 分钟 EMA 后入场胜率高", "txn_a1")
    k2 = _share_key_of("BTC", "入场", "回踩 30 分钟 EMA 后入场胜率高", "txn_a1")
    check("share_key 确定性(内容哈希)", k1 == k2 and k1.startswith("l-"))

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
