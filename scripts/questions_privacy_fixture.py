#!/usr/bin/env python3
"""Create planted-PI synthetic challenge examples, not independent held-out privacy labels."""

import argparse
import hashlib
import json
from pathlib import Path


def fixture():
    examples = {
        "en": [
            ("person", "Alice Demo needs ORDA in 4D v20.", "Alice Demo"),
            ("email", "Contact alice@example.invalid about the API.", "alice@example.invalid"),
            ("phone", "My phone is +1 202 555 0100; use JSON.", "+1 202 555 0100"),
            ("path", "My database is /Users/alice/private/db; keep ORDA.", "/Users/alice/private/db"),
            ("account", "My personal account customer_alice_42 needs access.", "customer_alice_42"),
            ("address", "My home is 42 Example Lane, Exampletown; 4D runs there.", "42 Example Lane, Exampletown"),
            ("username", "ａｌｉｃｅｐｒｉｖａｔｅ reports a problem in 4D v20.", "ａｌｉｃｅｐｒｉｖａｔｅ"),
            ("negative", "Use ORDA, ds.Employee.query(), SET QUERY AND LOCK and 4D v20.", None),
            ("negative-url", "Public product docs: https://developer.4d.com/docs/API/DataClass .", None),
        ],
        "fr": [
            ("person", "Élodie Démo utilise ORDA dans 4D v20.", "Élodie Démo"),
            ("email", "Mon adresse est elodie@example.invalid pour l'API.", "elodie@example.invalid"),
            ("phone", "Mon téléphone est +33 1 23 45 67 89; utilisez JSON.", "+33 1 23 45 67 89"),
            ("path", "Ma base est dans /Users/elodie/prive/base; utilisez ORDA.", "/Users/elodie/prive/base"),
            ("account", "Mon compte personnel client_elodie_42 doit être activé.", "client_elodie_42"),
            ("address", "J'habite au 42 rue Exemple, Villeexemple; j'utilise 4D.", "42 rue Exemple, Villeexemple"),
            ("username", "ａｌｉｃｅｐｒｉｖａｔｅ signale un problème dans 4D v20.", "ａｌｉｃｅｐｒｉｖａｔｅ"),
            ("negative", "Utilisez ORDA, ds.Employee.query(), SET QUERY AND LOCK et 4D v20.", None),
            ("negative-url", "Documentation publique : https://developer.4d.com/docs/API/DataClass .", None),
        ],
        "ja": [
            ("person", "山田太郎は4D v20でORDAを使用します。", "山田太郎"),
            ("email", "個人の連絡先はtaro@example.invalidです。APIを使用します。", "taro@example.invalid"),
            ("phone", "私の電話番号は+81 3 1234 5678です。JSONを使います。", "+81 3 1234 5678"),
            ("path", "私のデータベースは/Users/taro/private/dbにあります。ORDAを使用します。", "/Users/taro/private/db"),
            ("account", "個人アカウントcustomer_taro_42を有効にしてください。", "customer_taro_42"),
            ("address", "自宅の住所は例県例市例町42番です。4Dを使っています。", "例県例市例町42番"),
            ("username", "ａｌｉｃｅｐｒｉｖａｔｅが4D v20の問題を報告しています。", "ａｌｉｃｅｐｒｉｖａｔｅ"),
            ("negative", "ORDA、ds.Employee.query()、SET QUERY AND LOCK、4D v20を使用します。", None),
            ("negative-url", "公開製品ドキュメント：https://developer.4d.com/docs/API/DataClass 。", None),
        ],
    }
    cases = []
    for lang, rows in examples.items():
        for kind, text, planted in rows:
            spans = [] if planted is None else [{
                "start": text.index(planted), "end": text.index(planted) + len(planted),
                "text": planted, "kind": kind,
            }]
            cases.append({"id": lang + "-" + kind, "lang": lang, "text": text,
                          "posts": [{"user": "AlicePrivate", "text": "Synthetic question only."}],
                          "expected_spans": spans})
    return {"schema": "questions-local-pi-gold-v1", "scope": "synthetic-planted-v1",
            "independent_heldout": False, "held_out_from_prompt_tuning": False, "used_for_tool_diagnosis": True,
            "label_provenance": "deterministic planted labels authored in the same implementation stream",
            "real_corpus_recall_claim_allowed": False, "cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    data = (json.dumps(fixture(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with args.out.open("xb") as stream:
        stream.write(data)
    args.out.chmod(0o600)
    print(json.dumps({"cases": 27, "sha256": hashlib.sha256(data).hexdigest(), "scope": "synthetic-planted-v1"}))
