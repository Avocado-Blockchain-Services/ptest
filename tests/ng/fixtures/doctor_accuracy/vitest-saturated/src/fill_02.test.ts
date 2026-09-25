import { describe, expect, it } from 'vitest';
import { add } from './helpers';

describe('fill02', () => {
  it('adds', () => {
    expect(add(1, 2)).toBe(3);
  });
});
