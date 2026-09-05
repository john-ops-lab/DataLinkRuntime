# Third-Party Notices

DataLinkRuntime is licensed under Apache License 2.0. The Template Gallery introduced by Issue #132 contains independently written Recipe source and DLR-authored local tile compositions built from geometric styling and bundled Ant Design icon glyphs. Vendor and product names identify interoperability targets; no vendor Logo artwork is redistributed.

## Bundled icon glyphs

The local Template Gallery tiles use glyphs from `@ant-design/icons` 5.6.1 and its `@ant-design/icons-svg` 4.5.0 data package. Both packages declare the MIT License and originate from the [ant-design/ant-design-icons](https://github.com/ant-design/ant-design-icons) project.

> MIT LICENSE
>
> Copyright (c) 2018-present Ant UED, https://xtech.antfin.com/
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Compatible reference projects

The following fixed sources were reviewed under licenses that permit clean adaptation with the applicable attribution and change-notice obligations:

- [turbot/steampipe-plugin-alicloud](https://github.com/turbot/steampipe-plugin-alicloud), revision `d619a9d57505ae99aa5329aad3f4802ee94fde56`, Apache-2.0.
- [TencentCloud/steampipe-plugin-tencentcloud](https://github.com/TencentCloud/steampipe-plugin-tencentcloud), revision `d3a0a66fc6f67dd6ce805417efe3bc83a80bb587`. The repository LICENSE text states Apache-2.0 while the GitHub API reported `NOASSERTION`; this discrepancy is intentionally retained for review.
- [dlt-hub/dlt](https://github.com/dlt-hub/dlt), revision `3efddd61b9f85592bc71879ad0ede8a82d2de3d6`, Apache-2.0.

No source code from these projects is copied into the initial `reference-generated` Recipe set. They are listed for audit transparency. If a future catalog version directly adapts code, that change must identify exact files, preserve required copyright and NOTICE text, and document modifications.

## Official API metadata

- [aliyun/aliyun-openapi-meta](https://github.com/aliyun/aliyun-openapi-meta), revision `96914d57f79fe2228efe501bc40148715fcb1f77`, Apache-2.0. The Gallery uses pinned operation-schema facts only; no implementation code or fixture is copied. The exact 20 canonical files are linked from the source coverage matrix, and the frozen repository has no NOTICE file.

## Behavior-research-only sources

The following projects contributed only high-level behavior or product-scope research. Their code, structure, comments and fixtures are not copied or mechanically translated:

- [open-c3/open-c3](https://github.com/open-c3/open-c3), revision `039b9a42fdc80f31520ec0918000b8c7a05162e5`, GPL-2.0.
- [1Panel-dev/CloudExplorer](https://github.com/1Panel-dev/CloudExplorer), revision `aede557444bcf9d8daa49f5bb13e19cfaa43ce5f`, GPL-3.0.
- [airbytehq/airbyte](https://github.com/airbytehq/airbyte), revision `6f59bc9217670d69e4904adb4910b870e3eaf67c`, Elastic License 2.0 at the repository root.
- [yvain13/ServiceNow-CMDB-MCP](https://github.com/yvain13/ServiceNow-CMDB-MCP), revision `9f0fe8fd4792c6ee0e78fa3c74f40ddbe72feb61`; no repository LICENSE was found at the frozen revision.

## Suggested runtime dependencies

Template metadata may list exact Python, npm or Maven packages. Copying a Template does not install or redistribute those packages in the Control image. Operators must review and accept each dependency's license and security posture before installing it on a Worker. The authoritative package list is stored per Variant in the catalog metadata.

The complete source, license, use-mode, API and coverage record is maintained in [docs/templates/source-coverage-matrix.md](docs/templates/source-coverage-matrix.md).
