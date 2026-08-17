# Task: Fault Injection Tests

Create a pytest test file `tests/test_fault_injection_governed_executor.py` with tests for:

1. Inject fault before lease acquisition
2. Inject fault after journal recording  
3. Inject fault during recovery
4. Verify system recovers properly after each fault

Use the FaultInjector class from governed_executor.py. Keep tests simple and focused.