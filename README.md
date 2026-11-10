# Sparklet 模型目录

Sparklet 桌面端和服务从这里读取可用模型的资料，不包含模型权重。

| 分类 | 含义 |
| --- | --- |
| 兼容 | 在 Sparklet 上测试过，至少有一份通过的测试记录 |
| 推荐 | 兼容模型中人工挑选的少量模型，优先推荐使用 |

兼容只说明记录中的硬件、版本和条件通过了测试，不能外推到其他设备、上下文或并发。

## 文件

```text
catalog/
  models/<id>.json              每个模型一个文件，便于阅读和比较
  reports/<id>/<report>.json    测试记录
  v2/index.json                 客户端入口：序号、日期、模型与报告的 SHA-256
  v2/objects/<sha256>.json      按内容命名的不可变副本，与上面的文件逐字节相同
schemas/                        文件格式
tools/validate.py               校验工具（只用 Python 标准库）
```

## 内存信息怎么读

每个模型有一个或多个运行配置（`profiles`），对应一种硬件、推理引擎和并发。`memory` 列出每个上下文长度的占用：`gpuGiB` 是进程 GPU 内存峰值，`systemGiB` 是进程系统内存峰值，`commitGiB` 是 Windows 系统提交量。`basis` 说明数字的来源：

- `measured`：实测，`reportId` 指向对应的测试记录。
- `vendor`：模型发布方的说明。
- `estimated`：按权重和 KV cache 公式估算，并未实测。

未知的值为 `null`，不写 0。占用会随驱动、版本和负载变化，这些数字用来规划，不是保证。

## 这个仓库由程序写入

这里的文件由 Sparklet 模型工作台生成并发布，不要手工修改，手工改动会被下一次发布覆盖。模型问题、测试结果和建议请提交 Issue。
