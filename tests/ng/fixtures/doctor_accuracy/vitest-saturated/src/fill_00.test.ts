import { describe, expect, it } from 'vitest';
import { add } from './helpers';

describe('fill00', () => {
  it('adds', () => {
    expect(add(1, 2)).toBe(3);
  });
});
