#!/usr/bin/env python3

import aiohttp
import asyncio
import json
import time
from datetime import datetime

# API endpoint
API_URL = "http://localhost:8080/chat"

# Test prompts data for Health with ATP
prompts = [
    {
        "name": "Sarah Johnson",
        "number": "+91-98765-43210",
        "place": "Kerala, India",
        "interested_program": "PCOD Control Program",
        "time": "10:00 AM",
        "date": "2024-01-15"
    },
    {
        "name": "Priya Sharma",
        "number": "+91-98765-43211",
        "place": "Kerala, India",
        "interested_program": "Postpartum Control Program",
        "time": "2:30 PM",
        "date": "2024-01-16"
    },
    {
        "name": "Fatima Al-Mansoori",
        "number": "+971-50-123-4567",
        "place": "Dubai, UAE",
        "interested_program": "Thyroid Management Program",
        "time": "11:15 AM",
        "date": "2024-01-17"
    },
    {
        "name": "Aisha Khan",
        "number": "+971-50-123-4568",
        "place": "Dubai, UAE",
        "interested_program": "Mommy Pooch Program",
        "time": "3:45 PM",
        "date": "2024-01-18"
    },
    {
        "name": "Meera Nair",
        "number": "+91-98765-43212",
        "place": "Kerala, India",
        "interested_program": "Lifestyle Prevention Program",
        "time": "9:00 AM",
        "date": "2024-01-19"
    },
    {
        "name": "Linda Wilson",
        "number": "+971-50-123-4569",
        "place": "Dubai, UAE",
        "interested_program": "Menopause Support Program",
        "time": "1:30 PM",
        "date": "2024-01-20"
    },
    {
        "name": "Anjali Menon",
        "number": "+91-98765-43213",
        "place": "Kerala, India",
        "interested_program": "PCOD Control Program",
        "time": "4:00 PM",
        "date": "2024-01-21"
    },
    {
        "name": "Mariam Al-Hassani",
        "number": "+971-50-123-4570",
        "place": "Dubai, UAE",
        "interested_program": "Postpartum Control Program",
        "time": "10:30 AM",
        "date": "2024-01-22"
    },
    {
        "name": "Divya Krishnan",
        "number": "+91-98765-43214",
        "place": "Kerala, India",
        "interested_program": "Thyroid Management Program",
        "time": "2:00 PM",
        "date": "2024-01-23"
    },
    {
        "name": "Sofia Abdullah",
        "number": "+971-50-123-4571",
        "place": "Dubai, UAE",
        "interested_program": "Mommy Pooch Program",
        "time": "11:45 AM",
        "date": "2024-01-24"
    },
    {
        "name": "Rashida Begum",
        "number": "+91-98765-43215",
        "place": "Kerala, India",
        "interested_program": "Lifestyle Prevention Program",
        "time": "3:15 PM",
        "date": "2024-01-25"
    },
    {
        "name": "Noura Al-Maktoum",
        "number": "+971-50-123-4572",
        "place": "Dubai, UAE",
        "interested_program": "Menopause Support Program",
        "time": "9:30 AM",
        "date": "2024-01-26"
    },
    {
        "name": "Lakshmi Iyer",
        "number": "+91-98765-43216",
        "place": "Kerala, India",
        "interested_program": "PCOD Control Program",
        "time": "1:00 PM",
        "date": "2024-01-27"
    },
    {
        "name": "Hind Al-Qassimi",
        "number": "+971-50-123-4573",
        "place": "Dubai, UAE",
        "interested_program": "Postpartum Control Program",
        "time": "4:30 PM",
        "date": "2024-01-28"
    },
    {
        "name": "Sunita Patel",
        "number": "+91-98765-43217",
        "place": "Kerala, India",
        "interested_program": "Thyroid Management Program",
        "time": "10:15 AM",
        "date": "2024-01-29"
    },
    {
        "name": "Amira Al-Balooshi",
        "number": "+971-50-123-4574",
        "place": "Dubai, UAE",
        "interested_program": "Mommy Pooch Program",
        "time": "2:45 PM",
        "date": "2024-01-30"
    },
    {
        "name": "Radhika Gopal",
        "number": "+91-98765-43218",
        "place": "Kerala, India",
        "interested_program": "Lifestyle Prevention Program",
        "time": "11:00 AM",
        "date": "2024-01-31"
    },
    {
        "name": "Khadija Al-Muhairi",
        "number": "+971-50-123-4575",
        "place": "Dubai, UAE",
        "interested_program": "Menopause Support Program",
        "time": "3:30 PM",
        "date": "2024-02-01"
    },
    {
        "name": "Pooja Natarajan",
        "number": "+91-98765-43219",
        "place": "Kerala, India",
        "interested_program": "PCOD Control Program",
        "time": "9:45 AM",
        "date": "2024-02-02"
    },
    {
        "name": "Latifa Al-Shamsi",
        "number": "+971-50-123-4576",
        "place": "Dubai, UAE",
        "interested_program": "Postpartum Control Program",
        "time": "1:15 PM",
        "date": "2024-02-03"
    }
]

def create_prompt_message(prompt_data):
    """Create a formatted message from prompt data"""
    message = f"""
Hello! I'm {prompt_data['name']} and I'm interested in your {prompt_data['interested_program']} program.

Here are my details:
- Name: {prompt_data['name']}
- Phone: {prompt_data['number']}
- Location: {prompt_data['place']}
- Interested Program: {prompt_data['interested_program']}
- Preferred Time: {prompt_data['time']}
- Date: {prompt_data['date']}

Can you provide me with more information about this program and the application process?
"""
    return message.strip()

async def test_agent_api_async(session, prompt_data, prompt_num):
    """Test the agent API asynchronously with a single prompt"""
    try:
        message = create_prompt_message(prompt_data)
        
        payload = {
            "message": message,
            "user_id": f"user_{prompt_num}",
            "session_id": f"session_{prompt_num}"
        }
        
        print(f"\n🚀 Testing Prompt {prompt_num}: {prompt_data['name']}")
        print(f"📱 Phone: {prompt_data['number']}")
        print(f"📍 Location: {prompt_data['place']}")
        print(f"🎓 Program: {prompt_data['interested_program']}")
        print(f"⏰ Time: {prompt_data['time']} on {prompt_data['date']}")
        print("-" * 50)
        
        start_time = time.time()
        async with session.post(API_URL, json=payload, timeout=30) as response:
            end_time = time.time()
            
            print(f"⏱️  Response Time: {end_time - start_time:.2f} seconds")
            print(f"📊 Status Code: {response.status}")
            
            if response.status == 200:
                result = await response.json()
                print("✅ Success!")
                print(f"🤖 Agent Response: {result.get('content', 'No content field')[:200]}...")
                return True
            else:
                error_text = await response.text()
                print(f"❌ Error: {response.status}")
                print(f"📝 Error Details: {error_text}")
                return False
        
    except asyncio.TimeoutError:
        print("⏰ Timeout: Request took too long")
        return False
    except aiohttp.ClientConnectorError:
        print("🔌 Connection Error: Could not connect to the agent")
        return False
    except Exception as e:
        print(f"💥 Unexpected Error: {str(e)}")
        return False

def test_agent_api(prompt_data, prompt_num):
    """Synchronous wrapper for async test function"""
    return asyncio.run(test_agent_api_single(prompt_data, prompt_num))

async def test_agent_api_single(prompt_data, prompt_num):
    """Test single prompt asynchronously"""
    async with aiohttp.ClientSession() as session:
        return await test_agent_api_async(session, prompt_data, prompt_num)

async def run_agent_tests_async():
    """Run tests for all prompts asynchronously"""
    print("🎯 Starting Agent API Tests (Async)")
    print(f"🌐 Testing against: {API_URL}")
    print(f"📅 Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    successful_tests = 0
    failed_tests = 0
    
    # Create a session for all requests
    async with aiohttp.ClientSession() as session:
        # Create tasks for all prompts
        tasks = []
        for i, prompt in enumerate(prompts, 1):
            task = test_agent_api_async(session, prompt, i)
            tasks.append(task)
        
        # Run all tasks concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        for i, result in enumerate(results, 1):
            if isinstance(result, Exception):
                print(f"💥 Exception in prompt {i}: {str(result)}")
                failed_tests += 1
            elif result:
                successful_tests += 1
            else:
                failed_tests += 1
            
            print("-" * 60)
    
    # Summary
    print("\n📊 TEST SUMMARY")
    print("=" * 60)
    print(f"✅ Successful Tests: {successful_tests}")
    print(f"❌ Failed Tests: {failed_tests}")
    print(f"📈 Success Rate: {(successful_tests / len(prompts)) * 100:.1f}%")
    print(f"🏁 Completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    return successful_tests, failed_tests

def run_agent_tests():
    """Synchronous wrapper for async test runner"""
    return asyncio.run(run_agent_tests_async())

def test_single_prompt(prompt_number):
    """Test a single prompt by number"""
    if 1 <= prompt_number <= len(prompts):
        prompt = prompts[prompt_number - 1]
        print(f"🎯 Testing Single Prompt #{prompt_number}")
        return test_agent_api(prompt, prompt_number)
    else:
        print(f"❌ Invalid prompt number. Please use 1-{len(prompts)}")
        return False

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--single":
        if len(sys.argv) > 2:
            prompt_num = int(sys.argv[2])
            test_single_prompt(prompt_num)
        else:
            print("Usage: python test_agent_api.py --single <prompt_number>")
    else:
        run_agent_tests()
