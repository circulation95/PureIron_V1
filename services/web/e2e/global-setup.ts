// Playwright globalSetup: compose 스택을 올리고, 돌려준 함수(globalTeardown)로 내린다.
import { startStack, stopStack } from "./stack";

export default async function globalSetup(): Promise<() => void> {
  const stack = await startStack();
  return () => {
    stopStack(stack);
  };
}
