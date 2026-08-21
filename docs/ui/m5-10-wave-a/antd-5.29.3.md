# M5.10 Wave A: Ant Design 5.29.3 upstream contract

This file records the versioned knowledge entry used by the Wave A Web baseline. It is a query contract, not a copy of Ant Design's component documentation.

## Fixed versions

| Item | Version / boundary |
| --- | --- |
| React | 19.x |
| Ant Design | `antd@5.29.3` (exact package dependency) |
| ProComponents | `@ant-design/pro-components@2.8.10` (exact stable 2.8.x dependency) |
| React 19 compatibility | `@ant-design/v5-patch-for-react-19@1.0.3` (official v5 compatibility entry) |
| Ant Design CLI | `@ant-design/cli@6.6.1` for the versioned offline queries below |

`@ant-design/pro-components@2.8.10` declares the peer range `antd ^4.24.15 || ^5.11.2`, which includes the fixed `antd@5.29.3` baseline. The 3.x ProComponents line belongs to the Ant Design 6 migration and is outside this Wave.

## Official upstream sources

- [Ant Design LLMs.txt / Codex guidance](https://ant.design/docs/react/llms/)
- [Ant Design official CLI](https://ant.design/docs/react/cli/)
- [Official Codex skill source](https://github.com/ant-design/ant-design-cli/tree/main/skills/antd)
- [Versioned Ant Design 5 component overview](https://5x.ant.design/components/overview/)
- [Ant Design 5 ConfigProvider](https://5x.ant.design/components/config-provider/)
- [Ant Design 5 React 19 compatibility](https://5x.ant.design/docs/react/v5-for-19/)
- [Ant Design theme and Design Token guidance](https://ant.design/docs/react/customize-theme/)
- [Ant Design 5 changelog](https://5x.ant.design/components/changelog/)
- [ProComponents 2.x to 3.x version boundary](https://procomponents.ant.design/docs/migration-guide/)

The project-local official skill is `.agents/skills/antd/SKILL.md`. It was installed by the official CLI setup flow; the surrounding project rule fixes the CLI and `antd` versions so an unversioned latest lookup cannot silently change this baseline.

## Required query form

Run these commands before writing or reviewing an Ant Design API use. Keep structured output (`--format json`) so the query can be audited or repeated:

```bash
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info <Component> --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 doc <Component> --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 demo <Component> <demo-name> --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 token [<Component>] --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 semantic <Component> --format json
npx --yes @ant-design/cli@6.6.1 changelog 5.29.3 --format json
```

The query families are deliberately explicit:

- `info` / `doc`: exact props, types, defaults, deprecations, and full API text;
- `demo`: upstream runnable examples for the selected component;
- `token`: global or component Design Tokens in the v5 snapshot;
- `semantic`: `classNames` / `styles` Semantic DOM keys and structure;
- `changelog`: the exact `5.29.3` release record or a version-range diff.

The CLI bundles the selected metadata locally after installation; it does not replace the official docs links above or authorize a dependency upgrade.

## Wave A implementation boundary

Wave A only establishes this knowledge entrance, the exact dependency boundary, a thin `ConfigProvider` / token entry, the repeatable real-browser baseline, and an audit inventory. It does not migrate pages, replace assistant-ui, change routing, or modify the #90 identity, Session, Owner, or ACL contracts.
