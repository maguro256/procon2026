import numpy as np
import random
class worker:
    def __init__(self,worker_id):
        self.worker_id=worker_id
        self.skill=[random.random() for _ in range(3)]
        self.year=random.randrange(0,30)
        self.learning_rate=0.001

    def update_skill(self,task_type):
        self.skill[task_type]+=self.learning_rate*(1-self.skill[task_type])

    def get_context(self,taskdifficulty):
        year_norm=self.year/30
        return [year_norm,taskdifficulty,year_norm*taskdifficulty]

    def simulate_output(self ,task_type):
        time_taken=1000/(self.skill[task_type]+np.random.normal(loc=1,scale=0.5))#一旦秒とする
        feedback_list=[0,1,2] #1:easy 2:normal 3:hard
        feedback=random.choice(feedback_list)
        return time_taken,feedback
    
class RewardConverter:
    def __init__(self):
        self.feedback_reward=[1.5,1.0,0.5]
        self.task_avgtime_list=np.zeros(3)
        self.tasked_count=np.zeros(3)
        self.learningdata=[0,0,0]
    def to_reward(self,feedback,time_taken,task_type):
        time_score=self.return_task_avg(task_type,time_taken)[task_type]/time_taken
        reward=self.feedback_reward[feedback]*time_score
        self.recordReward(task_type,reward)
        return reward

    def return_task_avg(self,task_type,taken_time):
        avg_time=self.update_avg(task_type,taken_time)
        
        self.task_avgtime_list[task_type]=avg_time
        return self.task_avgtime_list
    
    def update_avg(self,task_type,taken_time):
        self.tasked_count[task_type]+=1
        current_count=self.tasked_count[task_type]
        
        current_avg=self.task_avgtime_list[task_type]
        avg=(current_avg*(current_count-1)+taken_time)/current_count
        return avg
    
    def recordReward(self,choicetask,reward):
        self.learningdata[choicetask]=(self.learningdata[choicetask]*self.tasked_count[choicetask]+reward)/self.tasked_count

class elorating:
    #試験的
    def __init__(self,n_workers=5,n_task_type=3):
        self.rating=np.zeros((n_workers,n_task_type))
        
    def update_rating(self,worker_id,task_type,reward):
        self.rating[worker_id][task_type]+=reward
        

class Simulator:
    def __init__(self,n_workers=5,n_task_type=3):
        self.workers=[worker(i) for i in range(n_workers)]
        self.task_difficulty=[0.15,0.3,0.45,0.6,0.85]       #タスク難易度1~5
        self.step_count=0
        self.current_task_type=None
    def get_context(self):
        contexts=[]
        self.current_task_type=random.randrange(len(self.task_difficulty))
        for i in range(len(self.workers)):
            difficulty=self.task_difficulty[self.current_task_type]
            contexts.append(self.workers[i].get_context(difficulty))
        return contexts,self.current_task_type

"""
sim=Simulator()
context,task_type=sim.get_context()
print("tasks:",task_type)
for i,c in enumerate(context):
    print(f"worker{i}:{c}")

w=worker(worker_id=0)
print("初期スキル:",w.skill)
w.update_skill(task_type=0)
print("タスク終了後:",w.skill)
"""


sim=Simulator()
w=worker(worker_id=0)
r=RewardConverter()
elo=elorating(n_workers=5,n_task_type=3)
feedbacked=["easy","normal","hard"]
for i in range(10000):
    choicetask=random.randrange(0,3)
    timetaken,feedback=w.simulate_output(choicetask)
    reward=r.to_reward(feedback,timetaken,choicetask)
    print(f"{timetaken} {feedbacked[feedback]} {choicetask}")
    print(reward)
    elo.update_rating(worker_id=0, task_type=choicetask, reward=reward)
print("elo rating:",elo.rating)
print(w.skill)

    
   




