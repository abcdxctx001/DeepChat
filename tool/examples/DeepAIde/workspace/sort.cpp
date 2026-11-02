#include<bits/stdc++.h>
using namespace std;
#define N 200010
int n,q,a[N],nxt[N],bel[N],pos[N],siz[N],c[N];
struct qu{
	int id,t,i;
};
stack<int> s;
void upd(int x,int val){
	for(;x<=n;x+=x&-x) c[x]+=val;
}
int query(int x){
	int res=0;
	for(;x;x-=x&-x) res+=c[x];
	return res;
}
int find(int x){
	//树状数组上二分，找到最后一个<n/2的位置
	
}
int main(){
	scanf("%d%d",&n,&q);
	for(int i=1;i<=n;i++){
		scanf("%d",&a[i]);
		pos[a[i]]=i;
	}
	pos[n+1]=n+1;
	s.push(n+1);
	for(int i=n;i>=1;i--){
		while(s.top()<a[i]) s.pop();
		nxt[a[i]]=s.top();
		s.push(a[i]);
	}
	for(int i=1;i<=n/2;){
		int j=min(n/2+1,pos[nxt[a[i]]]);
		siz[a[i]]=j-i;
		upd(a[i],siz[i]);
		for(int k=i;k<j;k++) bel[a[k]]=a[i];
		i=j;
	}
	for(int i=n/2+1;i<=n;){
		int j=pos[nxt[a[i]]];
		siz[a[i]]=j-i;
		upd(a[i],siz[i]);
		for(int k=i;k<j;k++) bel[a[k]]=a[i];
		i=j;
	}
	while(1){
		
	}
}