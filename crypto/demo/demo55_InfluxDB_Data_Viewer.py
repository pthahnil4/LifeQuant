#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
InfluxDB Data Viewer and Checker
===============================

Functions:
1. Query NEAR-USDT-SWAP K-line data
2. Query NEAR-USDT-SWAP technical indicator data
3. Format output for data verification
4. Support filtering by time period

Author: AI Assistant
Created: January 2025
"""

import sys
import os
from datetime import datetime, timezone
import time

# Add project path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import configuration
from influxdb_config import InfluxDBConfig

# Import InfluxDB client
try:
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.query_api import QueryApi
    print("✅ InfluxDB 2.x client library imported successfully")
except ImportError as e:
    print("❌ InfluxDB client library import failed")
    print("Please install: pip install influxdb-client")
    sys.exit(1)

class InfluxDBDataViewer:
    """InfluxDB data query and verification tool"""
    
    def __init__(self):
        print("🔧 Initializing InfluxDB data viewer...")
        
        # Load InfluxDB configuration
        print("📋 Loading InfluxDB configuration...")
        self.config = InfluxDBConfig()
        self.url = self.config.URL
        self.token = self.config.TOKEN
        self.org = self.config.ORG
        self.bucket = self.config.DEFAULT_BUCKETS[0]  # Use first bucket "garble"
        print(f"✅ InfluxDB configuration loaded")
        print(f"   URL: {self.url}")
        print(f"   Organization: {self.org}")
        print(f"   Target bucket: {self.bucket}")
        
        # Data table configuration
        self.kline_measurement = "near_usdt_swap_kline"      # K-line data table
        self.indicator_measurement = "near_usdt_swap_indicators"  # Technical indicator table
        self.inst_id = "NEAR-USDT"
        self.time_periods = ["5m", "15m", "1H", "4H", "1D", "1W"]
        
        # InfluxDB client
        self.client = None
        self.query_api = None
        
        print(f"🔧 Data viewer initialization complete")
        print(f"   K-line table: {self.kline_measurement}")
        print(f"   Indicator table: {self.indicator_measurement}")
        print(f"   Instrument: {self.inst_id}")
        
    def connect_influxdb(self):
        """Connect to InfluxDB"""
        try:
            print(f"\n🔗 Connecting to InfluxDB...")
            
            # Create client using configuration
            client_config = self.config.get_client_config()
            self.client = InfluxDBClient(**client_config)
            print("✅ InfluxDB client created successfully")
            
            # Test connection
            print("🔍 Testing InfluxDB connection...")
            health = self.client.health()
            print(f"✅ InfluxDB connection successful")
            print(f"   Status: {health.status}")
            print(f"   Version: {health.version}")
            
            # Initialize API
            print("🔧 Initializing InfluxDB query API...")
            self.query_api = self.client.query_api()
            print("✅ InfluxDB query API initialized")
            
            return True
            
        except Exception as e:
            print(f"❌ InfluxDB connection failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def query_kline_data(self, period_filter=None, limit=10):
        """Query K-line data"""
        try:
            print(f"\n📊 Querying K-line data...")
            print(f"   Data table: {self.kline_measurement}")
            print(f"   Bucket: {self.bucket}")
            
            # Build query conditions
            period_condition = ""
            if period_filter:
                period_condition = f'|> filter(fn: (r) => r.period == "{period_filter}")'
                print(f"   Period filter: {period_filter}")
            else:
                print(f"   Period filter: None (query all periods)")
            
            # Flux query statement
            query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -7d)
                |> filter(fn: (r) => r._measurement == "{self.kline_measurement}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                {period_condition}
                |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> sort(columns: ["_time"], desc: true)
                |> limit(n: {limit})
            '''
            
            print(f"🔍 Executing query (latest {limit} records)...")
            query_start = time.time()
            result = self.query_api.query(org=self.org, query=query)
            query_time = time.time() - query_start
            
            print(f"✅ Query completed, elapsed time: {query_time:.2f}s")
            
            records = []
            for table in result:
                for record in table.records:
                    records.append(record)
            
            if records:
                print(f"✅ Found {len(records)} K-line records")
                self._display_kline_data(records)
                return True
            else:
                print(f"⚠️ No K-line data found")
                return False
                
        except Exception as e:
            print(f"❌ K-line data query failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def query_indicator_data(self, period_filter=None, limit=10):
        """Query technical indicator data"""
        try:
            print(f"\n📈 Querying technical indicator data...")
            print(f"   Data table: {self.indicator_measurement}")
            print(f"   Bucket: {self.bucket}")
            
            # Build query conditions
            period_condition = ""
            if period_filter:
                period_condition = f'|> filter(fn: (r) => r.period == "{period_filter}")'
                print(f"   Period filter: {period_filter}")
            else:
                print(f"   Period filter: None (query all periods)")
            
            # Flux query statement
            query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -7d)
                |> filter(fn: (r) => r._measurement == "{self.indicator_measurement}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                {period_condition}
                |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
                |> sort(columns: ["_time"], desc: true)
                |> limit(n: {limit})
            '''
            
            print(f"🔍 Executing query (latest {limit} records)...")
            query_start = time.time()
            result = self.query_api.query(org=self.org, query=query)
            query_time = time.time() - query_start
            
            print(f"✅ Query completed, elapsed time: {query_time:.2f}s")
            
            records = []
            for table in result:
                for record in table.records:
                    records.append(record)
            
            if records:
                print(f"✅ Found {len(records)} technical indicator records")
                self._display_indicator_data(records)
                return True
            else:
                print(f"⚠️ No technical indicator data found")
                return False
                
        except Exception as e:
            print(f"❌ Technical indicator query failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _display_kline_data(self, records):
        """Format and display K-line data"""
        print(f"\n📊 K-line Data Details:")
        print("=" * 140)
        print(f"{'Time':<20} {'Period':<6} {'Open':<12} {'High':<12} {'Low':<12} {'Close':<12} {'Volume':<15} {'Vol_CCY':<15} {'Confirm':<6}")
        print("-" * 140)
        
        for record in records:
            time_str = record.get_time().strftime('%Y-%m-%d %H:%M:%S')
            period = record.values.get('period', 'N/A')
            open_val = record.values.get('open', 0)
            high_val = record.values.get('high', 0)
            low_val = record.values.get('low', 0)
            close_val = record.values.get('close', 0)
            volume_val = record.values.get('volume', 0)
            vol_ccy_val = record.values.get('vol_ccy', 0)
            confirm_val = record.values.get('confirm', 0)
            
            print(f"{time_str:<20} {period:<6} {open_val:<12.4f} {high_val:<12.4f} {low_val:<12.4f} {close_val:<12.4f} "
                  f"{volume_val:<15.2f} {vol_ccy_val:<15.2f} {'✅' if confirm_val == 1 else '❌':<6}")
        
        print("-" * 140)
        
        # Display data validity check
        print(f"\n🔍 Data Validity Check:")
        valid_count = 0
        price_count = 0
        for record in records:
            open_val = record.values.get('open', 0)
            high_val = record.values.get('high', 0)
            low_val = record.values.get('low', 0)
            close_val = record.values.get('close', 0)
            
            if open_val > 0 and high_val > 0 and low_val > 0 and close_val > 0:
                price_count += 1
                # Check price logic: high >= max(open,close) and low <= min(open,close)
                if high_val >= max(open_val, close_val) and low_val <= min(open_val, close_val):
                    valid_count += 1
        
        print(f"   Valid price records: {price_count}/{len(records)}")
        print(f"   Price logic correct: {valid_count}/{len(records)}")
        
        if valid_count == len(records):
            print(f"   ✅ All K-line data price logic is correct!")
        else:
            print(f"   ⚠️ Found {len(records) - valid_count} records with potential price logic issues")
    
    def _display_indicator_data(self, records):
        """Format and display technical indicator data"""
        print(f"\n📈 Technical Indicator Data Details:")
        print("=" * 180)
        print(f"{'Time':<20} {'Period':<6} {'Close':<10} {'MACD-EMA':<12} {'MA12':<10} {'MA26':<10} "
              f"{'MACD':<10} {'RSI':<8} {'BOLL_UP':<10} {'BOLL_MID':<10} {'BOLL_LOW':<10} {'KDJ_K':<8} {'KDJ_D':<8}")
        print("-" * 180)
        
        for record in records:
            time_str = record.get_time().strftime('%Y-%m-%d %H:%M:%S')
            period = record.values.get('period', 'N/A')
            close_val = record.values.get('close', 0)
            macd_ema_val = record.values.get('macd_ema', 0)
            ma12_val = record.values.get('ma12', 0)
            ma26_val = record.values.get('ma26', 0)
            macd_val = record.values.get('macd', 0)
            rsi_val = record.values.get('rsi', 0)
            boll_upper_val = record.values.get('boll_upper', 0)
            boll_middle_val = record.values.get('boll_middle', 0)
            boll_lower_val = record.values.get('boll_lower', 0)
            k_val = record.values.get('k', 0)
            d_val = record.values.get('d', 0)
            
            print(f"{time_str:<20} {period:<6} {close_val:<10.4f} {macd_ema_val:<12.6f} {ma12_val:<10.4f} {ma26_val:<10.4f} "
                  f"{macd_val:<10.6f} {rsi_val:<8.2f} {boll_upper_val:<10.4f} {boll_middle_val:<10.4f} {boll_lower_val:<10.4f} "
                  f"{k_val:<8.2f} {d_val:<8.2f}")
        
        print("-" * 180)
        
        # Display technical indicator statistics
        print(f"\n🔍 Technical Indicator Statistics:")
        non_zero_indicators = {}
        indicator_fields = ['ma12', 'ma26', 'ema12', 'ema26', 'dif', 'dea', 'macd', 'ema', 'macd_ema', 
                           'boll_upper', 'boll_middle', 'boll_lower', 'rsi', 'sar', 'k', 'd', 'j']
        
        for field in indicator_fields:
            non_zero_count = 0
            for record in records:
                val = record.values.get(field, 0)
                if val != 0:
                    non_zero_count += 1
            non_zero_indicators[field] = non_zero_count
        
        for field, count in non_zero_indicators.items():
            status = "✅" if count > 0 else "❌"
            print(f"   {field:<12}: {count}/{len(records)} non-zero values {status}")
        
        # Check indicator reasonableness
        print(f"\n🔍 Indicator Reasonableness Check:")
        reasonable_count = 0
        for record in records:
            rsi_val = record.values.get('rsi', 0)
            boll_upper = record.values.get('boll_upper', 0)
            boll_middle = record.values.get('boll_middle', 0)
            boll_lower = record.values.get('boll_lower', 0)
            close_val = record.values.get('close', 0)
            
            # Check if RSI is within 0-100 range
            rsi_ok = 0 <= rsi_val <= 100
            # Check BOLL channel order
            boll_ok = boll_upper >= boll_middle >= boll_lower > 0 if boll_lower > 0 else True
            
            if rsi_ok and boll_ok:
                reasonable_count += 1
        
        print(f"   Indicator reasonableness: {reasonable_count}/{len(records)}")
        if reasonable_count == len(records):
            print(f"   ✅ All technical indicator data is reasonable!")
        else:
            print(f"   ⚠️ Found {len(records) - reasonable_count} records that may be unreasonable")
    
    def query_data_counts(self):
        """Query data statistics for each table"""
        try:
            print(f"\n📊 Querying data statistics...")
            
            # Query K-line data statistics
            kline_query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -30d)
                |> filter(fn: (r) => r._measurement == "{self.kline_measurement}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                |> group(columns: ["period"])
                |> count()
            '''
            
            # Query technical indicator statistics
            indicator_query = f'''
            from(bucket: "{self.bucket}")
                |> range(start: -30d)
                |> filter(fn: (r) => r._measurement == "{self.indicator_measurement}")
                |> filter(fn: (r) => r.inst_id == "{self.inst_id}")
                |> group(columns: ["period"])
                |> count()
            '''
            
            print(f"🔍 Querying K-line data statistics...")
            kline_result = self.query_api.query(org=self.org, query=kline_query)
            
            print(f"🔍 Querying technical indicator statistics...")
            indicator_result = self.query_api.query(org=self.org, query=indicator_query)
            
            # Process K-line statistics results
            kline_stats = {}
            for table in kline_result:
                for record in table.records:
                    period = record.values.get('period', 'unknown')
                    field = record.values.get('_field', 'unknown')
                    count = record.values.get('_value', 0)
                    if period not in kline_stats:
                        kline_stats[period] = {}
                    kline_stats[period][field] = count
            
            # Process technical indicator statistics results
            indicator_stats = {}
            for table in indicator_result:
                for record in table.records:
                    period = record.values.get('period', 'unknown')
                    field = record.values.get('_field', 'unknown')
                    count = record.values.get('_value', 0)
                    if period not in indicator_stats:
                        indicator_stats[period] = {}
                    indicator_stats[period][field] = count
            
            # Display statistics results
            print(f"\n📊 Data Statistics Report:")
            print("=" * 100)
            print(f"{'Period':<8} {'K-line Records':<15} {'Indicator Records':<18} {'Status':<10}")
            print("-" * 100)
            
            all_periods = set(list(kline_stats.keys()) + list(indicator_stats.keys()))
            
            for period in sorted(all_periods):
                # K-line data count (use max count of all fields as representative)
                kline_count = 0
                if period in kline_stats:
                    kline_count = max(kline_stats[period].values()) if kline_stats[period] else 0
                
                # Technical indicator count (use max count of all fields as representative)
                indicator_count = 0
                if period in indicator_stats:
                    indicator_count = max(indicator_stats[period].values()) if indicator_stats[period] else 0
                
                status = "✅Complete" if kline_count > 0 and indicator_count > 0 else "⚠️Incomplete"
                
                print(f"{period:<8} {kline_count:<15} {indicator_count:<18} {status:<10}")
            
            print("-" * 100)
            return True
            
        except Exception as e:
            print(f"❌ Data statistics query failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def run_full_check(self, period_filter=None, limit=10):
        """Run complete data verification process"""
        print("\n" + "="*100)
        print("🔍 InfluxDB Data Verification Process")
        print("="*100)
        print(f"⏰ Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📊 Instrument: {self.inst_id}")
        print(f"📈 Period filter: {period_filter or 'All'}")
        print(f"🔢 Query limit: {limit} records")
        
        try:
            # 1. Connect to InfluxDB
            print(f"\n🔄 Step 1: Connect to InfluxDB")
            if not self.connect_influxdb():
                print(f"❌ Unable to connect to InfluxDB, verification terminated")
                return False
            
            # 2. Query data statistics
            print(f"\n🔄 Step 2: Query data statistics")
            self.query_data_counts()
            
            # 3. Query K-line data
            print(f"\n🔄 Step 3: Query K-line data details")
            kline_success = self.query_kline_data(period_filter, limit)
            
            # 4. Query technical indicator data
            print(f"\n🔄 Step 4: Query technical indicator data details")
            indicator_success = self.query_indicator_data(period_filter, limit)
            
            # 5. Display summary
            print(f"\n" + "="*100)
            print("📋 Data Verification Summary")
            print("="*100)
            print(f"   InfluxDB connection: ✅ Success")
            print(f"   K-line data query: {'✅ Success' if kline_success else '❌ Failed'}")
            print(f"   Technical indicator query: {'✅ Success' if indicator_success else '❌ Failed'}")
            
            if kline_success and indicator_success:
                print(f"✅ Data verification complete! Both K-line and technical indicator data exist")
            elif kline_success:
                print(f"⚠️ Only K-line data found, missing technical indicator data")
            elif indicator_success:
                print(f"⚠️ Only technical indicator data found, missing K-line data")
            else:
                print(f"❌ No data found")
            
            print(f"⏰ Completion time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            return True
            
        except Exception as e:
            print(f"❌ Data verification process failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def close(self):
        """Close connections"""
        if self.client:
            self.client.close()
            print("🔒 InfluxDB connection closed")

def main():
    """Main function"""
    print("🎯 Starting InfluxDB Data Viewer and Checker")
    viewer = InfluxDBDataViewer()
    
    try:
        # Configuration parameters
        period_filter = None  # Set to "5m", "1H" etc. to filter specific period, None for all
        limit = 5  # Number of records to query for each data type
        
        print(f"\n📋 Query configuration:")
        print(f"   Period filter: {period_filter or 'All periods'}")
        print(f"   Records per data type: {limit}")
        
        # Run complete verification process
        success = viewer.run_full_check(period_filter=period_filter, limit=limit)
        
        if success:
            print(f"\n🎉 Data verification completed!")
            print(f"💡 Tip: Review the output above to verify data correctness")
        else:
            print(f"\n⚠️ Data verification failed, please check InfluxDB connection and data")
            
    except KeyboardInterrupt:
        print(f"\n⏹️ User interrupted the program")
    except Exception as e:
        print(f"\n💥 Program exception: {e}")
        import traceback
        traceback.print_exc()
    finally:
        viewer.close()
        print("👋 Program ended")

if __name__ == "__main__":
    main()
