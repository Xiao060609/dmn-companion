# 工作流：在哪儿改代码、怎么发布

> 这份说明写给以后接手的人（包括我自己），避免每次都重新想一遍。

## 两个目录，各是什么

```
<工作区>/纯净版/     ← Git 仓库，**日常改代码在这里**
                      已推送 https://github.com/<你的账号>/dmn-companion

<工作区>/主项目/     ← 素材 / 依赖 / 真实数据的老家
                      只在本地，永不推送
```

（`<工作区>` 是你自己机器上的位置，这里不写死路径 ——
换机器时只需要建好那两个目录和下面的联接。）

### 为什么是两个

纯净版要能推送，就不能含这些东西：

| 内容 | 体积 | 为什么不能推 |
|---|---|---|
| `desktop/assets/` Live2D 素材 | 5.6 GB | 第三方版权，不许再分发 |
| `qq/astrbot/` 框架 | 2.4 GB | AGPL 第三方项目 |
| `qq/SnowLuma/`、`qq/qq-bridge/` | 208 MB | 第三方 |
| `desktop/desktop/node_modules/` | 330 MB | 依赖，`npm install` 能装回来 |
| `gateway/data/` 真实记忆库 | 3 MB | 有你和她的真实对话 |

### 但纯净版要能跑起来

所以这 6 个目录做成了**目录联接（junction）**，指向主项目：

```
desktop\assets               -> <工作区>/主项目/desktop/assets
desktop\desktop\node_modules -> <工作区>/主项目/desktop/desktop/node_modules
qq\astrbot                   -> <工作区>/主项目/qq/astrbot
qq\SnowLuma                  -> <工作区>/主项目/qq/SnowLuma
qq\qq-bridge                 -> <工作区>/主项目/qq/qq-bridge
gateway\data                 -> <工作区>/主项目/gateway/data
```

**含义**：在纯净版里读写这些目录，实际动的就是主项目里的那份。
不存在"两份数据要同步"的问题。

它们全在 `.gitignore` 里，`git status` 永远是干净的。

---

## 日常怎么改

直接在纯净版目录里改。跑测试：

```powershell
cd <工作区>\纯净版
<venv>\Scripts\python.exe -m pytest gateway/tests -q
<venv>\Scripts\python.exe -m pytest desktop/tests -q
```

改完发布：

```powershell
git status                     # 先看清楚改了什么
git add -A
git commit -m "说明"
git push
```

---

## ⚠ 提交前必做：脱敏检查

**这是这个项目最容易出事的一步。**

真实信息散在代码和文档里。发布前跑：

```powershell
<venv>\Scripts\python.exe tools\check_privacy.py
```

它会检查：真实昵称、QQ 号、群名、真实聊天记录、本机路径、疑似密钥。
有命中就**别推**，先改掉。

**已经推上去的东西删不掉** —— git 历史里永远留着，别人 `git log -S` 能翻出来。

---

## 历史上踩过的坑（别重犯）

1. **`git add -A` 把 Chrome 临时配置目录收了进去**
   220 个文件，含 Cookies / Login Data。
   → `.gitignore` 已加 `audit/_chrome*`。

2. **测试把生产 `config.json` 写坏了**
   根因：`OWNER_IDS` 是导入期快照，测试在导入之后才隔离目录。
   → 加了 `WRITE_CONFIG_MIRROR` 开关，测试里关掉回写。

3. **`.bak` 备份文件混进仓库**
   → `.gitignore` 里排除了 `*.bak`。

4. **缺运行时数据文件导致 39 条测试挂**
   纯净版曾漏了 `gateway/memory/data/hit_stopwords.txt`（停用词表）。
   它不是 `.py`，之前的同步清单没覆盖到。
   → 现在这个文件已在仓库里，**别删**。

---

## 如果联接丢了

搬动主项目之后联接会失效。重建（不需要管理员权限）：

```powershell
# 先删掉失效的
Remove-Item <工作区>\纯净版\desktop\assets -Force

# 再重建
New-Item -ItemType Junction `
  -Path <工作区>\纯净版\desktop\assets `
  -Target <工作区>\主项目\desktop\assets
```

其余 5 个同理，路径见上面那张表。

---

## 公开仓库的边界

- **不含**：第三方源码、模型权重、Live2D 素材、真实记忆库
- **含**：本项目自己写的全部代码、测试、文档、审计报告
- 许可证：本项目 MIT；第三方各有其许可，见 `THIRD-PARTY-NOTICES.md`
