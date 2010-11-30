import itertools

class BoundingBox (object):
    
    def __init__(self, origin = (0,0,0), size = (0,0,0)):
        self._origin, self._size = list(map(int, origin)),list(map(int, size))
    
    def getMinx(self): return self.origin[0];
    def getMiny(self): return self.origin[1];
    def getMinz(self): return self.origin[2];
    
    def getMaxx(self): return self.origin[0]+self.size[0];
    def getMaxy(self): return self.origin[1]+self.size[1];
    def getMaxz(self): return self.origin[2]+self.size[2];
    
    def setMinx(self, x):
        self.size[0] -= x - self.origin[0]
        self.origin[0] = x;
        
    def setMiny(self,y):
        self.size[1] -= y - self.origin[1]
        self.origin[1] = y
        
    def setMinz(self,z):
        self.size[2] -= z - self.origin[2]
        self.origin[2] = z
        
    
    def setMaxx(self, x):
        if x < self.origin[0]:
            x = self.origin[0];
             
        self.size[0] = x - self.origin[0]
        
    def setMaxy(self, y): 
        if y < self.origin[1]:
            y = self.origin[1];
             
        self.size[1] = y - self.origin[1]
        
    def setMaxz(self, z): 
        if z < self.origin[2]:
            z = self.origin[2];
             
        self.size[2] = z - self.origin[2]
        
    
    minx = property(getMinx, setMinx);
    miny = property(getMiny, setMiny);
    minz = property(getMinz, setMinz);
    
    maxx = property(getMaxx, setMaxx);
    maxy = property(getMaxy, setMaxy);
    maxz = property(getMaxz, setMaxz);
    
    def getMincx(self): return self.origin[0]>>4;
    def getMincz(self): return self.origin[2]>>4;
    
    def getMaxcx(self): return ((self.origin[0]+self.size[0]-1)>>4)+1;
    def getMaxcz(self): return ((self.origin[2]+self.size[2]-1)>>4)+1;
    
    mincx = property(getMincx, None, None, "The smallest chunk position contained in this box");
    mincz = property(getMincz, None, None, "The smallest chunk position contained in this box");
    
    maxcx = property(getMaxcx, None, None, "The largest chunk position contained in this box");
    maxcz = property(getMaxcz, None, None, "The largest chunk position contained in this box");
    
    def getOrigin(self): return self._origin;
    def setOrigin(self, o): self._origin = list(o);
    
    def getSize(self): return self._size;
    def setSize(self, s): self._size = list(s);
    
    origin = property(getOrigin, setOrigin)
    size = property(getSize, setSize)
    
    def getWidth(self): return self._size[0];
    def getHeight(self): return self._size[1];
    def getLength(self): return self._size[2];
    
    def setWidth(self, w): self.size[0] = int(w);
    def setHeight(self, h): self.size[1] = int(h);
    def setLength(self, l): self.size[2] = int(l);
    
    width = property(getWidth, setWidth, None, "The dimension along the X axis");
    height = property(getHeight, setHeight, None, "The dimension along the Y axis");
    length = property(getLength, setLength, None, "The dimension along the Z axis");
    
    
    def getMaximum(self): return map(lambda a,b:a+b, self._origin, self._size)
    
    maximum = property(getMaximum, None, None, "The endpoint of the box; origin plus size.")
    
    def getVolume(self): return reduce(lambda a,b:a*b, self.size)
    volume = property(getVolume, None, None, "The volume of the box in blocks")
    
    @property
    def chunkPositions(self):
        #iterate through all of the chunk positions within this selection box
        return itertools.product(xrange(self.mincx,self.maxcx), xrange(self.mincz, self.maxcz));
        
    
    @property
    def isChunkAligned(self):
        return (self.origin[0] & 0xf == 0) and (self.origin[2] & 0xf == 0)
    
    def __contains__(self, pos):
        x,y,z = pos;
        if x<self.minx or x>=self.maxx: return False
        if y<self.miny or y>=self.maxy: return False
        if z<self.minz or z>=self.maxz: return False
        
        return True;
    
    def __cmp__(a, b):
        return cmp( (a.origin, a.size), (b.origin, b.size) )
        
    def __repr__(self):
        return "BoundingBox({0}, {1})".format(self.origin, self.size)
