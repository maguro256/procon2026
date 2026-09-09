import numpy as np
import random


TASK_COUNT=5
class worker:
    def __init__(self,worker_id):
        self.worker_id=worker_id
        self.skill=[random.random() for _ in range(5)]
        self.year=random.randrange(0,30)
        self.learning_rate=0.001

    def update_skill(self,task_type):
        self.skill[task_type]+=self.learning_rate*(1-self.skill[task_type])

    def get_context(self,taskdifficulty,task_type):
        year_norm=self.year/30
        tasklist=[0]*TASK_COUNT
        tasklist[task_type]=1
        context=[year_norm,taskdifficulty,year_norm*taskdifficulty]+tasklist
        return context

    def simulate_output(self ,task_type):
        time_taken=1000/(self.skill[task_type]+np.random.normal(loc=1,scale=0.1))#一旦秒とする
        feedback_list=[0,1,2] #0:easy 1:normal 2:hard
        feedback=random.choice(feedback_list)
        return time_taken,feedback #feedbackは仮
    
class RewardConverter:
    def __init__(self):
        self.feedback_reward=[1.5,1.0,0.5]
        self.task_avgtime_list=np.zeros(TASK_COUNT)
        self.tasked_count=np.zeros(TASK_COUNT)
        self.learningdata=[0]*TASK_COUNT
    def to_reward(self,feedback,time_taken,task_type):
        timeavg=self.return_task_avg(task_type,time_taken)[task_type]
        time_score=timeavg/time_taken
        if timeavg*0.9>time_taken:
            feedback=0
        elif timeavg*1.1<time_taken:        #ここでfeedbackを設定
            feedback=2
        else :
            feedback=1
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
        self.learningdata[choicetask]=(self.learningdata[choicetask]*self.tasked_count[choicetask]+reward)/self.tasked_count[choicetask]

class workerBelief:
    def __init__(self):
        self.mu=np.zeros(3+TASK_COUNT)
        self.sigma=np.eye(3+TASK_COUNT)*100
        self.sigma_obs2=0.1
        
    def sample_theta(self):
        return np.random.multivariate_normal(self.mu,self.sigma)
        
    def predict(self,x,theta):
        return theta[0]*x[0]+theta[1]*x[1]+theta[2]*x[2]+theta[3]*x[3]+theta[4]*x[4]+theta[5]*x[5]+theta[6]*x[6]+theta[7]*x[7]
    
    def update(self,x,reward):
        x=np.array(x)
        sigma_inv_new=np.linalg.inv(self.sigma)+(1/self.sigma_obs2)*(np.outer(x,x))
        sigma_new=np.linalg.inv(sigma_inv_new)
        mu_new=sigma_new@(np.linalg.inv(self.sigma)@self.mu+(1/self.sigma_obs2)*x*reward)
        self.mu=mu_new
        self.sigma=sigma_new
        

class AssignmentEngine:
    def __init__(self):
        self.beliefs=[workerBelief(),workerBelief(),workerBelief(),workerBelief(),workerBelief()]
        
    def choice_worker(self,x,theta,i):
        return self.beliefs[i].predict(x,theta)
    
    

class Simulator:
    def __init__(self,n_workers=5,n_task_type=3):
        self.workers=[worker(i) for i in range(n_workers)]
        self.task_difficulty=[0.15,0.3,0.45,0.6,0.85]       #タスク難易度1~5#タスク数５に固定
        self.step_count=0
        self.current_task_type=None
    def get_context(self):
        contexts=[]
        self.current_task_type=random.randrange(len(self.task_difficulty))
        for i in range(len(self.workers)):
            difficulty=self.task_difficulty[self.current_task_type]
            contexts.append(self.workers[i].get_context(difficulty,self.current_task_type))
        return contexts,self.current_task_type



if __name__ == "__main__":
    # ここから下は AI.py 単体で回す検証用シミュレーション。
    # app.py からは ai_stub.py が上のクラスだけを import する。
    sim=Simulator()
    matrix = [[0 for _ in range(5)] for _ in range(5)]
    r=RewardConverter()
    ae=AssignmentEngine()
    #elo=elorating(n_workers=5,n_task_type=3)
    feedbacked=["easy","normal","hard"]
    for i in range(10000):
        choicetask=random.randrange(0,5)
        j=0
        index=0
        max_predict=float('-inf')
        for workers in sim.workers:
            predict= ae.choice_worker(workers.get_context(sim.task_difficulty[choicetask],choicetask),ae.beliefs[j].sample_theta(),j)
            if max_predict<predict:
                max_predict=predict
                index=j
            j=j+1
        matrix[choicetask][index]=matrix[choicetask][index]+1
        timetaken,feedback=sim.workers[index].simulate_output(choicetask)
        reward=r.to_reward(feedback,timetaken,choicetask)
        ae.beliefs[index].update(sim.workers[index].get_context(sim.task_difficulty[choicetask],choicetask),reward)
        print(f"選択タスク{choicetask}")
        print(f"選択された人{index}")




    for w in sim.workers:
        print("各workerSkill")
        print(w.worker_id, w.skill)
    for i, belief in enumerate(ae.beliefs):
        print("各workerMu")
        print(i, belief.mu)
    print("選ばれた回数,matrix[choicetask][index]")
    print(matrix)

