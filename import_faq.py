"""
FAQ完成版.xlsx の FAQシートから質問と回答を読み込み、
system_prompt.txt の【FAQ】セクションを上書き更新するスクリプト。

使い方:
    python import_faq.py
    python import_faq.py --xlsx path/to/FAQ完成版.xlsx --prompt path/to/system_prompt.txt
"""

import argparse
from pathlib import Path

import pandas as pd


XLSX_DEFAULT = "FAQ完成版.xlsx"
SHEET_NAME = "FAQ"
COL_QUESTION = "質問"
COL_ANSWER = "インフルエンサーへの回答内容"
PROMPT_DEFAULT = "system_prompt.txt"
FAQ_HEADER = "【FAQ】"


def load_faq(xlsx_path: Path) -> list[tuple[str, str]]:
    """Excel から (質問, 回答) のリストを返す。空行はスキップする。"""
    df = pd.read_excel(xlsx_path, sheet_name=SHEET_NAME, dtype=str)

    missing = [c for c in (COL_QUESTION, COL_ANSWER) if c not in df.columns]
    if missing:
        raise ValueError(f"列が見つかりません: {missing}（シート: {SHEET_NAME}）")

    rows = []
    for _, row in df.iterrows():
        q = str(row[COL_QUESTION]).strip()
        a = str(row[COL_ANSWER]).strip()
        if q and a and q != "nan" and a != "nan":
            rows.append((q, a))

    return rows


def build_faq_section(faq_rows: list[tuple[str, str]]) -> str:
    """FAQ リストを Q&A 形式のテキストブロックに変換する。"""
    lines = [FAQ_HEADER]
    for i, (q, a) in enumerate(faq_rows, start=1):
        lines.append(f"Q{i}. {q}")
        lines.append(f"A{i}. {a}")
        lines.append("")  # 空行で区切る
    return "\n".join(lines).rstrip()


def update_prompt(prompt_path: Path, faq_section: str) -> None:
    """system_prompt.txt の【FAQ】以降を新しい内容で置き換える。"""
    original = prompt_path.read_text(encoding="utf-8")

    faq_index = original.find(FAQ_HEADER)
    if faq_index == -1:
        raise ValueError(f"「{FAQ_HEADER}」が {prompt_path} に見つかりません。")

    updated = original[:faq_index] + faq_section
    prompt_path.write_text(updated, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="FAQ を system_prompt.txt に追記する")
    parser.add_argument("--xlsx", default=XLSX_DEFAULT, help="Excel ファイルのパス")
    parser.add_argument("--prompt", default=PROMPT_DEFAULT, help="system_prompt.txt のパス")
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx)
    prompt_path = Path(args.prompt)

    if not xlsx_path.exists():
        raise FileNotFoundError(f"Excel ファイルが見つかりません: {xlsx_path}")
    if not prompt_path.exists():
        raise FileNotFoundError(f"system_prompt.txt が見つかりません: {prompt_path}")

    print(f"読み込み中: {xlsx_path}")
    faq_rows = load_faq(xlsx_path)
    print(f"  → {len(faq_rows)} 件のFAQを取得しました")

    faq_section = build_faq_section(faq_rows)
    update_prompt(prompt_path, faq_section)

    print(f"更新完了: {prompt_path}")
    print("---- 書き込み内容プレビュー（先頭3件）----")
    for i, (q, a) in enumerate(faq_rows[:3], start=1):
        print(f"  Q{i}. {q}")
        print(f"  A{i}. {a}")
    if len(faq_rows) > 3:
        print(f"  ... 他 {len(faq_rows) - 3} 件")


if __name__ == "__main__":
    main()
