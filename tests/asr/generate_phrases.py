#!/usr/bin/env python3
"""Generate 100 deterministic work-related phrases for each benchmark language."""
import json
import random
from itertools import product

LANG_CONFIG = {
    "en": {
        "name": "English",
        "voice": "en-US-AriaNeural",
        "subjects": ["I", "We", "The team", "My manager", "The client"],
        "verbs": ["need to review", "will send", "have to finish", "want to schedule", "should update"],
        "objects": ["the budget report", "the project plan", "the client email", "the meeting agenda"],
        "times": ["before lunch", "by Friday", "this afternoon", "tomorrow morning"],
        "template": "{subject} {verb} {object} {time}.",
    },
    "zh": {
        "name": "Chinese Mandarin",
        "voice": "zh-CN-XiaoxiaoNeural",
        "subjects": ["我", "我们", "团队", "我的经理", "客户"],
        "verbs": ["需要审阅", "会发送", "必须完成", "想要安排", "应该更新"],
        "objects": ["预算报告", "项目计划", "客户邮件", "会议议程"],
        "times": ["午饭前", "周五前", "今天下午", "明天上午"],
        "template": "{subject}{verb}{object}{time}。",
    },
    "zh-tw": {
        "name": "Taiwan Mandarin",
        "voice": "zh-TW-HsiaoChenNeural",
        "subjects": ["我", "我們", "團隊", "我的經理", "客戶"],
        "verbs": ["需要審閱", "會寄送", "必須完成", "想要安排", "應該更新"],
        "objects": ["預算報告", "專案計畫", "客戶郵件", "會議議程"],
        "times": ["午餐前", "週五前", "今天下午", "明天上午"],
        "template": "{subject}{verb}{object}{time}。",
    },
    "es": {
        "name": "Spanish",
        "voice": "es-ES-ElviraNeural",
        "subjects": [
            "Tengo que",
            "Tenemos que",
            "El equipo tiene que",
            "Mi jefe tiene que",
            "El cliente tiene que",
        ],
        "verbs": ["revisar", "enviar", "terminar", "programar", "actualizar"],
        "objects": [
            "el informe de presupuesto",
            "el plan del proyecto",
            "el correo del cliente",
            "la agenda de la reunión",
        ],
        "times": ["antes del almuerzo", "para el viernes", "esta tarde", "mañana por la mañana"],
        "template": "{subject} {verb} {object} {time}.",
    },
    "tl": {
        "name": "Tagalog",
        "voice": "fil-PH-BlessicaNeural",
        "subjects": [
            "Kailangan kong",
            "Kailangan nating",
            "Kailangan ng koponan na",
            "Kailangan ng aking boss na",
            "Kailangan ng kliyente na",
        ],
        "verbs": ["suriin", "ipadala", "tapusin", "ayusin", "i-update"],
        "objects": [
            "ang ulat ng badyet",
            "ang plano ng proyekto",
            "ang email ng kliyente",
            "ang agenda ng pulong",
        ],
        "times": ["bago magtanghalian", "bago matapos ang Biyernes", "ngayong hapon", "bukas ng umaga"],
        "template": "{subject} {verb} {object} {time}.",
    },
}


def make_phrases(lang, count=100, seed=42):
    cfg = LANG_CONFIG[lang]
    combos = list(product(cfg["subjects"], cfg["verbs"], cfg["objects"], cfg["times"]))
    rng = random.Random(seed)
    rng.shuffle(combos)
    selected = combos[:count]
    phrases = []
    for i, (subj, verb, obj, time) in enumerate(selected, start=1):
        text = cfg["template"].format(subject=subj, verb=verb, object=obj, time=time)
        phrases.append({"id": f"{lang}_{i:03d}", "text": text})
    return phrases


def main():
    dataset = {}
    for lang in LANG_CONFIG:
        dataset[lang] = {
            "name": LANG_CONFIG[lang]["name"],
            "voice": LANG_CONFIG[lang]["voice"],
            "phrases": make_phrases(lang),
        }
    with open("phrases.json", "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)
    print("Wrote phrases.json with", sum(len(v["phrases"]) for v in dataset.values()), "entries")


if __name__ == "__main__":
    main()
