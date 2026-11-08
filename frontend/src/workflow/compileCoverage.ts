/**
 * Compile/Route step coverage.
 *
 * A disabled step is a deliberate skip: the Compile still contains it, but the
 * Route only orders enabled steps. Anything else that goes missing between the
 * authored project and the compiled steps is a bug, and the UI must block
 * "创建 Route" instead of silently running a shorter process.
 */
export type CoverageStep = { id: string; name?: string; enabled?: boolean };

export function enabledCoverageSteps(steps: CoverageStep[]): CoverageStep[] {
  return steps.filter(step => step.enabled !== false);
}

export function skippedCoverageSteps(steps: CoverageStep[]): CoverageStep[] {
  return steps.filter(step => step.enabled === false);
}

/** Enabled authored steps that the Compile did not map back to a compiled step. */
export function missingCompiledSteps(
  authored: CoverageStep[],
  compiledAuthoredIds: Iterable<string>,
): CoverageStep[] {
  const known = new Set(compiledAuthoredIds);
  return enabledCoverageSteps(authored).filter(step => !known.has(step.id));
}

/** Enabled compiled steps that the Route stepOrder does not cover. */
export function missingRouteSteps(expectedStepIds: string[], stepOrder: string[]): string[] {
  const covered = new Set(stepOrder);
  return expectedStepIds.filter(id => !covered.has(id));
}

export function coverageStepNames(steps: Array<{ id: string; name?: string }>): string {
  return steps.map(step => step.name?.trim() || step.id).join('、');
}
