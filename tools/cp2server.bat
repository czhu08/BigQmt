chcp 65001
xcopy /S /Y D:\delta43\BigQmt\src\bigqmt_signal_trader   D:\Programs\国金证券QMT交易端\python\bigqmt_signal_trader
copy /Y D:\delta43\BigQmt\src\bigqmt_signal_trader_strategy.py  D:\Programs\国金证券QMT交易端\python\
copy /Y D:\delta43\BigQmt\src\bigqmt_signal_trader_redis_rpc_runtime.py  D:\Programs\国金证券QMT交易端\python\
rem This terminal forbids strategy FileIO. Deploy the inert placeholder under the
rem retired filename so chart indicators stop raising Forbidden FileIO.
copy /Y D:\delta43\BigQmt\src\BIGQMT_REDIS_DRYRUN_CHART_STUB.py    D:\Programs\国金证券QMT交易端\python\BIGQMT_REDIS_DRYRUN.py
copy /Y D:\delta43\BigQmt\src\BIGQMT_ZMQ_DRYRUN.py    D:\Programs\国金证券QMT交易端\python\
copy /Y D:\delta43\BigQmt\src\bigqmt_signal_trader_local_config.py    D:\Programs\国金证券QMT交易端\python\
pause
