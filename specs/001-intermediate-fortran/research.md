# Research Log: Intermediate IR and Fortran Emission

## Overview

Establish the runtime, tooling, and validation workflows required to implement the staged `metdsl emit` CLI that produces deterministic IR packages and multi-compiler-ready Fortran 2003 artefacts.

## Findings

### 1. Python Runtime and CLI Framework
- **Decision**: Target Python 3.9+ with Typer for the CLI orchestration.
- **Rationale**: Python 3.9 is the minimum version that still receives security fixes and supports type annotations needed for maintainability. Typer builds on Click, enabling structured commands, rich help output, and async support if future stages require it.
- **Alternatives considered**:  
  - *Click directly*: More boilerplate for option parsing and help generation.  
  - *argparse*: Lacks sub-command ergonomics for staged workflows.

### 2. Configuration and Validation Layer
- **Decision**: Use Pydantic models to load and validate emission configuration files.
- **Rationale**: Pydantic enforces schema validation, supports hashing for configuration fingerprints, and integrates well with JSON/YAML inputs that scientists are likely to maintain.
- **Alternatives considered**:  
  - *marshmallow*: More verbose schema definitions.  
  - *Custom dataclasses + manual validation*: Higher maintenance burden, weaker error messaging.

### 3. Intermediate Representation Persistence
- **Decision**: Serialize IR packages as JSON documents with deterministic ordering and include the DSL model identifier + configuration hash as the canonical ID.
- **Rationale**: JSON is inspectable in governance workflows, diff-friendly for golden fixtures, and portable across tooling. Embedding the hash keeps reruns deterministic and guards against stale artefacts.
- **Alternatives considered**:  
  - *Binary formats (e.g., Protocol Buffers)*: Harder to audit and diff without tooling.  
  - *Plain text ad-hoc format*: Risk of ambiguity and brittle parsing.

### 4. Fortran Code Generation Strategy
- **Decision**: Template Fortran emission with Jinja2, emphasising compiler-friendly structure and inserting minimal inline comments around generated regions.
- **Rationale**: Jinja2 supports reusable template fragments, letting us mirror canonical Fortran layout while injecting DSL-derived content. Minimal comments satisfy audit needs without harming compiler optimisations.
- **Alternatives considered**:  
  - *AST-based Fortran builders*: Higher upfront complexity, limited libraries.  
  - *String concatenation*: Error-prone and difficult to maintain.

### 5. Multi-Compiler Validation Workflow
- **Decision**: Run sequential smoke tests using GNU Fortran 11+, Intel oneAPI Fortran, and NVIDIA NVFortran; capture success/failure metadata in the emission report.
- **Rationale**: Matches success criteria and governance expectations, providing parity across CPU and GPU-aligned toolchains. Sequential runs keep resource usage predictable and simplify log attribution.
- **Alternatives considered**:  
  - *Parallel compiler invocations*: Faster but complicates logging and resource contention on shared nodes.  
  - *Single reference compiler*: Fails governance expectations and cross-platform assurances.

### 6. Telemetry and Observability
- **Decision**: Emit structured NDJSON event stream (via Rich console logging + file sink) for each stage: `emit_started`, `ir_emitted`, `fortran_emitted`, `compiler_validated`, `emit_failed`.
- **Rationale**: Aligns with Constitution Principle V. NDJSON integrates cleanly with log aggregators, and event granularity supports the governance verification flow.
- **Alternatives considered**:  
  - *Unstructured stdout*: Harder to parse in automation.  
  - *Only final summary*: Insufficient for in-flight monitoring.

### 7. Discovery Hook for Future Targets
- **Decision**: Implement a metadata-only `--list-targets` hook that reports supported (Fortran 2003) and candidate experimental targets without enabling code generation.
- **Rationale**: Satisfies scope clarification (PoC hook only) while avoiding partial artefacts. Exposing metadata prepares users for roadmap discussions without overcommitting.
- **Alternatives considered**:  
  - *Enable experimental code generation*: Violates scope; risks unstable artefacts.  
  - *Omit hook entirely*: Fails to provide pathway visibility requested in spec.
