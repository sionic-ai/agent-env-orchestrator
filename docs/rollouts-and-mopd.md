# Rollouts, reinforcement learning, and MOPD

This document explains where an environment orchestrator fits into a learning system. It is **not a claim that this repository implements a trainer, MOPD, or a token-level training-data exporter**. The deterministic demo is a test fixture, not a learned agent.

## Environment, rollout, trajectory, update

- **Environment:** the versioned initial files/state, available tools, execution constraints, and verification contract.
- **Rollout:** one attempt made by the current policy (student model) through that environment. An agent generates actions, receives observations, and continues until termination.
- **Trajectory:** the recorded sequence of model actions and environment observations, plus termination information and resulting artifacts/state.
- **Learning update:** an external training algorithm uses suitable trajectories and learning signals to change the student model's parameters.

Running a model, saving its conversation, or computing a score alone is evaluation/data collection, not a parameter update. An orchestration service can support learning without owning GPU provisioning or optimization.

## Environment-reward agentic RL

A conceptual loop is:

1. Select a versioned task and create a clean session.
2. Sample one or more trajectories using the current student policy.
3. Stop agent execution and verify the resulting state or artifacts.
4. Distinguish valid task scores from environment/verifier failures.
5. Supply compatible trajectories and scores to an external RL trainer.
6. Update policy weights and produce a new policy version.
7. Generate new rollouts with that version.

Some algorithms compare multiple trajectories sampled for the same task. Their group identifiers and sampling configuration must therefore be recorded. Exact data requirements depend on the chosen algorithm.

## MOPD: teacher guidance on student-generated responses

MiMo-V2-Flash describes Multi-Teacher On-Policy Distillation (MOPD) as learning from the student's own generated responses with dense, token-level guidance from domain-specialist teachers. Its learning signal is derived from student/teacher distribution divergence.[1]

Conceptually:

1. The student generates a response or multi-turn trajectory.
2. A suitable teacher evaluates token probabilities conditioned on the student's actual preceding context.
3. The learning system compares student and teacher token distributions/probabilities.
4. The student is updated using the method's specified distillation/RL objective.

This differs from simply training on teacher-written answers: the student visits its own generated contexts. It also differs from an LLM judge returning a textual critique or one scalar score. A judge score is not a teacher token distribution.

Environment reward and teacher guidance are distinct learning signals. Do not assume a particular weighted sum, routing rule, or loss reproduces MiMo without checking the corresponding paper and implementation. Teacher guidance does not eliminate the need to validate tool behavior or harden a verifier.

## MiMo-V2.6: MOPD2

The MiMo-V2.6-Pro-RL model card describes Multi-Prefix Multi-Teacher On-Policy Distillation (MOPD2) **after mixed RL**. It combines autonomous student rollouts with prefix-conditioned single-turn student rollouts using Teacher-Prefix and SFT-Prefix histories. This lets training target a decision point without regenerating all preceding turns and extends the approach to hard-to-verify tasks.[2]

An environment runner should not silently label a resumed/prefix-conditioned run as a clean, autonomous run. The source of a prefix and the state-reconstruction contract need to be explicit. Prefix support is not implemented merely by exporting text logs.

## Integration boundary for this project

The environment side should supply reproducible environment/task identity, isolated execution, observations, final state/artifacts, verification outcome, and operational errors.

A future external rollout/training connector must additionally preserve, as required by its algorithm:

- policy/checkpoint version;
- tokenizer and chat-template versions;
- exact generated token IDs and generation configuration;
- masks identifying student-generated tokens versus observations/prefixes;
- rollout-policy log probabilities where required;
- teacher/model identity and compatible probability information for distillation;
- task/group/run identifiers and termination/truncation reasons.

Plain conversation JSON does not guarantee these properties. Arbitrary text-returning model APIs may not expose the teacher probabilities needed for MOPD. Any teacher/student tokenizer mismatch requires an explicitly defined alignment method.

The initial local demo proves environment lifecycle and verification mechanics only. Consult the repository's compatibility documentation for currently implemented capabilities.

## Sources

[1] https://github.com/XiaomiMiMo/MiMo-V2-Flash

[2] https://huggingface.co/XiaomiMiMo/MiMo-V2.6-Pro-RL
