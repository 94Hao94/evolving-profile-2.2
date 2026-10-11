import { expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { classifyPromptOrigin, promptPopulationProjection } from '@/lib/prompt-origin';
const fixtures: Array<{name:string;row:any;metadata:any;expected:string}> = JSON.parse(readFileSync(new URL('../../../guidance/prompt-origin-fixtures.json', import.meta.url), 'utf8'));
it.each(fixtures)('classifies native host metadata consistently with Python: $name', (fixture: any) => {
  expect(classifyPromptOrigin(fixture.row, fixture.metadata).origin_kind).toBe(fixture.expected);
});
it('keeps source filtering and denominators bound to the same classified rows', () => {
  const rows = [{ origin_kind: 'human' }, { origin_kind: 'unknown' }, { origin_kind: 'automation' }];
  expect(promptPopulationProjection(rows)).toMatchObject({ rows: [{ origin_kind: 'human' }], statistics_denominator: 1, natural_total: 1, audit_total: 3 });
  expect(promptPopulationProjection(rows, 'unknown').rows).toEqual([{ origin_kind: 'unknown' }]);
  expect(promptPopulationProjection(rows, 'all').statistics_denominator).toBe(3);
});
