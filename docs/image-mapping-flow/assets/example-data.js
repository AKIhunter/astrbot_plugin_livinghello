window.LHMappingExamples = {
  "scenarios": {
    "current": {
      "policy": {
        "contract": "livinghello.image-prompt-mapping.v1",
        "enabled": true,
        "retry_limit": 2,
        "learn_enabled": true,
        "limits": {
          "desc_soft": 400,
          "desc_hard": 500,
          "total_hard": 900
        },
        "scope": {
          "key": "01d2f3e388a931be9468588dd31d5a287cc20965d289d1954deee59fda1cd9cd",
          "provider_id": "offline-image",
          "model": "offline-example",
          "protocol": "images"
        },
        "learned_count": 0,
        "warning": "",
        "rule_count": 13,
        "rules_source": "file",
        "load_status": "loaded",
        "rules_file": "image_prompt_rules.json",
        "rules_mtime_ns": 1795153551629782474
      },
      "detail": "当前文件校验通过，13条规则可用。成功词对单独统计。",
      "log": "INFO [LH-img] 映射表 file=image_prompt_rules.json mtime=1795153551629782474 state=loaded source=file rules=13"
    },
    "previous": {
      "policy": {
        "contract": "livinghello.image-prompt-mapping.v1",
        "enabled": true,
        "retry_limit": 2,
        "learn_enabled": true,
        "limits": {
          "desc_soft": 400,
          "desc_hard": 500,
          "total_hard": 900
        },
        "scope": {
          "key": "01d2f3e388a931be9468588dd31d5a287cc20965d289d1954deee59fda1cd9cd",
          "provider_id": "offline-image",
          "model": "offline-example",
          "protocol": "images"
        },
        "learned_count": 0,
        "warning": "映射表无效：JSON语法错误（第1行，第2列）；使用上一份有效规则。",
        "rule_count": 13,
        "rules_source": "previous",
        "load_status": "invalid",
        "rules_file": "image_prompt_rules.json",
        "rules_mtime_ns": 1795153551633769422
      },
      "detail": "本次更新未生效，仍使用同路径上一份有效表的13条规则。",
      "log": "WARNING [LH-img] 映射表 file=image_prompt_rules.json mtime=1795153551633769422 state=invalid source=previous rules=13 reason=映射表无效：JSON语法错误（第1行，第2列）；使用上一份有效规则。"
    },
    "invalid": {
      "policy": {
        "contract": "livinghello.image-prompt-mapping.v1",
        "enabled": true,
        "retry_limit": 2,
        "learn_enabled": true,
        "limits": {
          "desc_soft": 400,
          "desc_hard": 500,
          "total_hard": 900
        },
        "scope": {
          "key": "01d2f3e388a931be9468588dd31d5a287cc20965d289d1954deee59fda1cd9cd",
          "provider_id": "offline-image",
          "model": "offline-example",
          "protocol": "images"
        },
        "learned_count": 0,
        "warning": "映射表无效：第1条规则：正则不支持分组、反向引用或复杂重复；使用内置规则。",
        "rule_count": 1,
        "rules_source": "builtin",
        "load_status": "invalid",
        "rules_file": "image_prompt_rules.json",
        "rules_mtime_ns": 1795153551635608750
      },
      "detail": "首次加载没有有效旧表，回退至1条内置规则。",
      "log": "WARNING [LH-img] 映射表 file=image_prompt_rules.json mtime=1795153551635608750 state=invalid source=builtin rules=1 reason=映射表无效：第1条规则：正则不支持分组、反向引用或复杂重复；使用内置规则。"
    },
    "missing": {
      "policy": {
        "contract": "livinghello.image-prompt-mapping.v1",
        "enabled": true,
        "retry_limit": 2,
        "learn_enabled": true,
        "limits": {
          "desc_soft": 400,
          "desc_hard": 500,
          "total_hard": 900
        },
        "scope": {
          "key": "01d2f3e388a931be9468588dd31d5a287cc20965d289d1954deee59fda1cd9cd",
          "provider_id": "offline-image",
          "model": "offline-example",
          "protocol": "images"
        },
        "learned_count": 0,
        "warning": "映射表不存在或无法访问，使用内置规则。",
        "rule_count": 1,
        "rules_source": "builtin",
        "load_status": "missing",
        "rules_file": "image_prompt_rules.json",
        "rules_mtime_ns": null
      },
      "detail": "找不到映射文件，使用1条内置规则；文件出现后会自动重新加载。",
      "log": "WARNING [LH-img] 映射表 file=image_prompt_rules.json mtime=None state=missing source=builtin rules=1 reason=映射表不存在或无法访问，使用内置规则。"
    },
    "disabled": {
      "policy": {
        "contract": "livinghello.image-prompt-mapping.v1",
        "enabled": false,
        "retry_limit": 2,
        "learn_enabled": true,
        "limits": {
          "desc_soft": 400,
          "desc_hard": 500,
          "total_hard": 900
        },
        "scope": {
          "key": "01d2f3e388a931be9468588dd31d5a287cc20965d289d1954deee59fda1cd9cd",
          "provider_id": "offline-image",
          "model": "offline-example",
          "protocol": "images"
        },
        "learned_count": 0,
        "warning": "",
        "rule_count": 0,
        "rules_source": "file",
        "load_status": "loaded",
        "rules_file": "image_prompt_rules.json",
        "rules_mtime_ns": 1795153551638607075
      },
      "detail": "映射表已加载，开关关闭后应用规则数为0；历史数据保留。",
      "log": "INFO [LH-img] 映射表 file=image_prompt_rules.json mtime=1795153551638607075 state=loaded source=file rules=13"
    },
    "recovered": {
      "policy": {
        "contract": "livinghello.image-prompt-mapping.v1",
        "enabled": true,
        "retry_limit": 2,
        "learn_enabled": true,
        "limits": {
          "desc_soft": 400,
          "desc_hard": 500,
          "total_hard": 900
        },
        "scope": {
          "key": "01d2f3e388a931be9468588dd31d5a287cc20965d289d1954deee59fda1cd9cd",
          "provider_id": "offline-image",
          "model": "offline-example",
          "protocol": "images"
        },
        "learned_count": 0,
        "warning": "",
        "rule_count": 13,
        "rules_source": "file",
        "load_status": "loaded",
        "rules_file": "image_prompt_rules.json",
        "rules_mtime_ns": 1795153551642802023
      },
      "detail": "修复JSON后校验通过，警告清除，恢复当前表的13条规则。",
      "log": "INFO [LH-img] 映射表 file=image_prompt_rules.json mtime=1795153551642802023 state=loaded source=file rules=13"
    }
  },
  "cases": [
    {
      "title": "普通等义用词",
      "prompt": "blue blouse",
      "source": "blouse",
      "target": "shirt",
      "manual": {
        "status": "ready",
        "prompt": "blue shirt"
      },
      "repair": {
        "status": "ready"
      },
      "learned": {
        "status": "ready",
        "prompt": "blue shirt"
      },
      "before": [],
      "after": []
    },
    {
      "title": "同义姿势表达",
      "prompt": "lying on her back",
      "source": "lying on her back",
      "target": "lying face up",
      "manual": {
        "status": "ready",
        "prompt": "lying face up"
      },
      "repair": {
        "status": "ready"
      },
      "learned": {
        "status": "ready",
        "prompt": "lying face up"
      },
      "before": [
        "lying:up"
      ],
      "after": [
        "lying:up"
      ]
    },
    {
      "title": "改变人物姿势",
      "prompt": "kneeling",
      "source": "kneeling",
      "target": "reclining",
      "manual": {
        "status": "review",
        "prompt": "kneeling"
      },
      "repair": {
        "status": "review"
      },
      "learned": {
        "status": "skipped",
        "prompt": "kneeling"
      },
      "before": [
        "kneel"
      ],
      "after": [
        "reclin"
      ]
    },
    {
      "title": "明确颜色语境",
      "prompt": "a nude color dress",
      "source": "nude",
      "target": "beige",
      "manual": {
        "status": "ready",
        "prompt": "a beige color dress"
      },
      "repair": {
        "status": "ready"
      },
      "learned": {
        "status": "ready",
        "prompt": "a beige color dress"
      },
      "before": [],
      "after": []
    },
    {
      "title": "改变人物年龄",
      "prompt": "an 18 year old adult",
      "source": "18",
      "target": "19",
      "manual": {
        "status": "review",
        "prompt": "an 18 year old adult"
      },
      "repair": {
        "status": "review"
      },
      "learned": {
        "status": "skipped",
        "prompt": "an 18 year old adult"
      },
      "before": [
        "1",
        "8",
        "adult"
      ],
      "after": [
        "1",
        "9",
        "adult"
      ]
    },
    {
      "title": "改变身体要点",
      "prompt": "abdomen",
      "source": "abdomen",
      "target": "navel",
      "manual": {
        "status": "review",
        "prompt": "abdomen"
      },
      "repair": {
        "status": "review"
      },
      "learned": {
        "status": "skipped",
        "prompt": "abdomen"
      },
      "before": [],
      "after": [
        "navel"
      ]
    }
  ]
};
