# Analyzer Prompt Suite - Index & Usage (v0.4.2)

One prompt per target type, all on the same v2 architecture:
Phase 0 classify -> Phase 1 Element Census (every raw code element gets an
ID) -> Phase 2-3 map census into the output schema -> Phase 4 count
reconciliation (hard gate) -> strict JSON with coverageReport. The census
+ reconciliation is what makes "nothing missed" VERIFIABLE: every element
must appear mapped or explicitly unmapped-with-reason, and totals must
add up arithmetically.

## Frontend / UI (Playwright runner - selectors, modals, toasts, inline errors)

| File | Use for |
|---|---|
| JSX_SCREEN_ANALYZER_PROMPT.md | React / React-based JSX/TSX screens (also React Native web-rendered) |
| ANALYZER_PROMPT_ANGULAR.md | Angular (Material, ng-bootstrap, PrimeNG; overlay/teleport-aware) |
| ANALYZER_PROMPT_VUE.md | Vue 2/3, Nuxt (Element Plus, Vuetify, Ant Design Vue; teleport-aware) |
| ANALYZER_PROMPT_SVELTE.md | Svelte / SvelteKit (incl. form actions, Svelte 5 runes) |
| ANALYZER_PROMPT_JSP.md | JSP / JSF / Struts / Spring-MVC server-rendered Java UI |

All four share the same output JSON schema -> one UI-ingestion parser.

## Backend / API (API test runner - endpoints, schemas, error catalogs)

| File | Use for |
|---|---|
| ANALYZER_PROMPT_PYTHON.md | FastAPI - Flask - Django/DRF - Celery - CLI |
| ANALYZER_PROMPT_JAVA_SPRINGBOOT.md | Spring Boot - plain Java (JAX-RS/servlets) |
| ANALYZER_PROMPT_KOTLIN.md | Ktor - Spring Boot in Kotlin (nullability-as-schema) |
| ANALYZER_PROMPT_CSHARP_DOTNET.md | ASP.NET Core MVC/Web API - Minimal APIs |
| ANALYZER_PROMPT_NODEJS_TYPESCRIPT.md | Express - Fastify - NestJS - Koa |
| ANALYZER_PROMPT_PHP.md | Laravel - Symfony - Slim - plain PHP |
| ANALYZER_PROMPT_RUBY_RAILS.md | Rails API - Sinatra - Grape |
| ANALYZER_PROMPT_GO.md | net/http - gin - echo - chi - fiber - mux |
| ANALYZER_PROMPT_RUST.md | axum - actix-web - rocket - warp |

All nine share the IDENTICAL endpoint JSON schema -> one backend parser.

## Systems & Data (different test surfaces, own schemas)

| File | Use for | Runner target |
|---|---|---|
| ANALYZER_PROMPT_C_CPP.md | C/C++ libraries, modules, CLIs | Unit-test generation (GoogleTest, Catch2, CUnit, Unity) - testableUnits schema |
| ANALYZER_PROMPT_SQL_STORED_PROCEDURES.md | Oracle PL/SQL - T-SQL - MySQL - PL/pgSQL packages, procs, triggers | Direct DB-call testing (utPLSQL, tSQLt, pgTAP, custom) - testableRoutines schema |

## How to run (applies to every prompt)

1. Pick the ONE prompt matching the file type - never mix; a universal
   prompt dilutes census rules and misses more.
2. Paste the prompt + ALL files of the feature/module INCLUDING support
   files: validation utils, error/exception classes, config, constants,
   i18n/message dictionaries, DDL (for SQL). Missing support files are
   the #1 cause of UNRESOLVED_KEY / missingFiles entries.
3. Run per feature-folder / per module, not whole repos - census output
   is verbose by design; large scopes hit output limits.
4. Spot-check `coverageReport.reconciliation` after every run: totals
   must equal mapped + unmapped per category. If a total looks low
   versus the code (3 routes censused, file clearly has 10) -> rerun;
   the census was cut short.
5. Best-accuracy setup (recommended for your orchestrator): TWO PASSES.
   Pass 1: "produce ONLY elementCensus for these files."
   Pass 2: files + census back in -> full JSON.
   Splitting enumeration from schema-filling is the single biggest
   precision gain available.

## Consistency guarantees

- Same phases, same reconciliation gate, same coverageReport shape in
  every prompt -> shared ingestion/validation code in your tool.
- UI prompts share one schema; backend prompts share one schema;
  C/C++ and SQL each have purpose-built schemas because their test
  surface is functions/routines, not HTTP or DOM.

## Coverage of "languages companies use" - honest status

Covered: React, Angular, Vue, Svelte - Python, Java, Kotlin, C#, Node/TS,
PHP, Ruby, Go, Rust - C/C++ - SQL stored procedures. That spans the
overwhelming majority of enterprise application code.

Deliberately NOT included (different runner architectures - tell me if
you want them added):
- Mobile native (Android Kotlin/Java UI, iOS Swift, Flutter, React
  Native native views) -> needs an Appium/Maestro-oriented schema, not
  Playwright selectors.
- Scala (Play/Akka), Elixir (Phoenix), Perl, COBOL -> addable on the
  same architecture if your clients' stacks need them.
- GraphQL: partially covered - resolvers written in any covered language
  get censused as routes/handlers, but a dedicated GraphQL
  schema-analysis prompt would test the SDL surface directly.

## Honest limits

- No prompt guarantees literal 100% - the architecture makes misses
  DETECTABLE (reconciliation arithmetic), not impossible. The guarantee
  comes from checking the coverage numbers.
- Accuracy is bounded by inputs: unresolved routes/messages surface as
  missingFiles/unresolvedTexts - supply the files and rerun.
- Highly dynamic code (routes from DB config, reflection-registered
  handlers) is censused as a "dynamic registration site" finding rather
  than guessed.
