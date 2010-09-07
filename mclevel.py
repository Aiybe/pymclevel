"""
MCLevel interfaces

Sample usage:

import mclevel

# Call mclevel.fromFile to identify and open any of these four file formats:
#
# Classic levels - gzipped serialized java objects.  Returns an instance of MCJavalevel
# Indev levels - gzipped NBT data in a single file.  Returns an MCIndevLevel
# Schematics - gzipped NBT data in a single file.  Returns an MCSchematic.  
#   MCSchematics have the special method rotateLeft which will reorient torches, stairs, and other tiles appropriately.
# Alpha levels - world folder structure containing level.dat and chunk folders.  Single or Multiplayer.
#   Can accept a path to the world folder or a path to the level.dat.  Returns an MCInfdevOldLevel

# Load a Classic level.
level = mclevel.fromFile("server_level.dat"); 

# fromFile identified the file type and returned a MCJavaLevel.  MCJavaLevel doesn't actually know any java. It guessed the
# location of the Blocks array by starting at the end of the file and moving backwards until it only finds valid blocks.
# It also doesn't know the dimensions of the level.  This is why you have to tell them to MCEdit via the filename.
# This works here too:  If the file were 512 wide, 512 long, and 128 high, I'd have to name it "server_level_512_512_128.dat"
#
# This is one area for improvement.

# Classic and Indev levels have all of their blocks in one place.
blocks = level.Blocks

# Sand to glass.
blocks[blocks == level.materials.materialNamed("Sand")] = level.materials.materialNamed("Glass")

# Save the file with another name.  This only works for non-Alpha levels.
level.saveToFile("server_level_glassy.dat");

# Load an Alpha world
# Loading an Alpha world immediately scans the folder for chunk files.  This takes longer for large worlds.
ourworld = mclevel.fromFile("C:\\Minecraft\\OurWorld");

# Convenience method to load a numbered world from the saves folder.
world1 = mclevel.loadWorldNumber(1);

# Find out which chunks are present
chunkPositions = world1.presentChunks

# presentChunks returns a list of tuples (xPos, zPos)
xPos, zPos = chunkPositions[0];

# retrieve an InfdevChunk object. getChunk is a special method;  
# it will load the chunk from disk, decompress it, inflate the NBT structures, and unpack the data arrays for you.
aChunk = world1.getChunk(xPos, zPos)

### Access the data arrays of the chunk like so:

# Fire to Leaves.
aChunk.Blocks[aChunk.Blocks==world.materials.materialNamed("Fire")] = world.materials.materialNamed("Leaves")

# Generate Flatgrass in this chunk

# Set all BlockData from height 64 up to 0.
# Take note that the array is indexed x, z, y.  The last index corresponds to height or altitude.  
# Also take note that the Data, BlockLight, and SkyLight arrays have been unpacked from 4-bit arrays to numpy uint8 arrays, 
# by the call to getChunk. This makes them much easier to work with.
aChunk.Data[:,:,64:] = 0;

# The chunk doesn't know you've changed any of that data.  Call chunkChanged() to let it know.
# This will mark the chunk for lighting calculation, recompression, and writing to disk.
# It will also immediately recalculate the chunk's HeightMap and fill the SkyLight only with light falling straight down. 
# These are relatively fast and were added here to aid MCEdit.
aChunk.chunkChanged();

# Don't call aChunk.save() unless you have a really good reason.  In fact, forget I mentioned it.

# To recalculate all of the dirty lights in the world, call generateLights
world.generateLights();

# Move the player and his spawn
world.setPlayerPosition( (0, 67, 0) ) # add 3 to make sure his head isn't in the ground.
world.setPlayerSpawnPosition( (0, 64, 0) )

# Save the level.dat and any chunks that have been marked for writing to disk
# This also compresses any chunks marked for recompression.
world.saveInPlace();


# Advanced use:
# The getChunkSlices method returns an iterator that returns slices of chunks within the specified range.
# the slices are returned as tuples of (chunk, slices, point)

# chunk:  The InfdevChunk object we're interested in.
# slices:  A 3-tuple of slice objects that can be used to index chunk's data arrays
# point:  A 3-tuple of floats representing the relative position of this subslice within the larger slice.
# 
# Take caution:
# the point tuple is ordered (x,y,z) in accordance with the tuples used to initialize a bounding box
# however, the slices tuple is ordered (x,z,y) for easy indexing into the arrays.

# Here is MCInfdevOldLevel.fillBlocks in its entirety:

def fillBlocks(self, box, blockType, blockData = 0):
    chunkIterator = self.getChunkSlices(box)
    
    for (chunk, slices, point) in chunkIterator:
        chunk.Blocks[slices] = blockType
        chunk.Data[slices] = blockData
        chunk.chunkChanged();


Copyright 2010 David Rio Vierra
"""

import nbt
from nbt import *
import gzip
import StringIO
from numpy import array, zeros, uint8
import itertools
import traceback
import os;
import sys;

from materials import *
from copy import deepcopy
import time
from datetime import datetime;
from box import BoundingBox

FaceXIncreasing = 0
FaceXDecreasing = 1
FaceYIncreasing = 2
FaceYDecreasing = 3
FaceZIncreasing = 4
FaceZDecreasing = 5
MaxDirections = 6

saveFileDirs = {#xxxx platform
    'win32':os.path.expandvars("%APPDATA%\\.minecraft\\saves"),
    'darwin':os.path.expanduser("~/Library/Application Support/Minecraft"),
}
saveFileDir = saveFileDirs.get(sys.platform, os.path.expanduser("~/.minecraft")); #default to Linux save location 
  
"""
Indev levels:

TAG_Compound "MinecraftLevel"
{
   TAG_Compound "Environment" 
   {
      TAG_Short "SurroundingGroundHeight"// Height of surrounding ground (in blocks)
      TAG_Byte "SurroundingGroundType"   // Block ID of surrounding ground
      TAG_Short "SurroundingWaterHeight" // Height of surrounding water (in blocks)
      TAG_Byte "SurroundingWaterType"    // Block ID of surrounding water
      TAG_Short "CloudHeight"            // Height of the cloud layer (in blocks)
      TAG_Int "CloudColor"               // Hexadecimal value for the color of the clouds
      TAG_Int "SkyColor"                 // Hexadecimal value for the color of the sky
      TAG_Int "FogColor"                 // Hexadecimal value for the color of the fog
      TAG_Byte "SkyBrightness"           // The brightness of the sky, from 0 to 100
   }
   
   TAG_List "Entities"
   {
      TAG_Compound
      {
         // One of these per entity on the map.
         // These can change a lot, and are undocumented.
         // Feel free to play around with them, though.
         // The most interesting one might be the one with ID "LocalPlayer", which contains the player inventory
      }
   }
   
   TAG_Compound "Map"
   {
      // To access a specific block from either byte array, use the following algorithm:
      // Index = x + (y * Depth + z) * Width

      TAG_Short "Width"                  // Width of the level (along X) 
      TAG_Short "Height"                 // Height of the level (along Y) 
      TAG_Short "Length"                 // Length of the level (along Z) 
      TAG_Byte_Array "Blocks"             // An array of Length*Height*Width bytes specifying the block types
      TAG_Byte_Array "Data"              // An array of Length*Height*Width bytes with data for each blocks
      
      TAG_List "Spawn"                   // Default spawn position
      {
         TAG_Short x  // These values are multiplied by 32 before being saved
         TAG_Short y  // That means that the actual values are x/32.0, y/32.0, z/32.0
         TAG_Short z
      }
   }
   
   TAG_Compound "About"
   {
      TAG_String "Name"                  // Level name
      TAG_String "Author"                // Name of the player who made the level
      TAG_Long "CreatedOn"               // Timestamp when the level was first created
   }
}
"""

#String constants for known tag names
MinecraftLevel = "MinecraftLevel"

Environment = "Environment"
SurroundingGroundHeight = "SurroundingGroundHeight"
SurroundingGroundType = "SurroundingGroundType"
SurroundingWaterHeight = "SurroundingWaterHeight"
SurroundingWaterType = "SurroundingWaterType"
CloudHeight = "CloudHeight"
CloudColor = "CloudColor"
SkyColor = "SkyColor"
FogColor = "FogColor"
SkyBrightness = "SkyBrightness"

Entities = "Entities"
TileEntities = "TileEntities"

Map = "Map"
Width = "Width"
Height = "Height"
Length = "Length"
Blocks = "Blocks"
Data = "Data"
Spawn = "Spawn"

#entities
Inventory = 'Inventory'
Motion = "Motion"
Pos = "Pos"
Rotation = "Rotation"

About = "About"
Name = "Name"
Author = "Author"
CreatedOn = "CreatedOn"

#infdev
Level = 'Level'
BlockData = 'BlockData'
BlockLight = 'BlockLight'
SkyLight = 'SkyLight'
HeightMap = 'HeightMap'
TerrainPopulated = 'TerrainPopulated'
LastUpdate = 'LastUpdate'
xPos = 'xPos'
zPos = 'zPos'

Data = 'Data'
SpawnX = 'SpawnX'
SpawnY = 'SpawnY'
SpawnZ = 'SpawnZ'
LastPlayed = 'LastPlayed'
RandomSeed = 'RandomSeed'
SizeOnDisk = 'SizeOnDisk' #maybe update this?
Time = 'Time'
Player = 'Player'

#schematic
Materials = 'Materials'

#decorator for the primitive methods of MCLevel.
def decompress_first(func):
    def dec_first(self, *args, **kw):
        self.decompress();
        return func(self, *args, **kw);
    return dec_first
    
class MCLevel:
    """ MCLevel is an abstract class providing many routines to the different level types, 
    including a common copyEntitiesFrom built on class-specific routines, and
    a dummy getChunk/getPresentChunks for the finite levels.
    
    MCLevel subclasses must have Width, Length, and Height attributes.  The first two are always zero for infinite levels.
    Subclasses must also have Blocks, and optionally Data and BlockLight.
    """
    
    ###common to Creative, Survival and Indev. these routines assume
    ###self has Width, Height, Length, and Blocks
   
    materials = classicMaterials;
    
    hasEntities = False;
    needsCompression = False;
    compressedTag = None
    root_tag = None
    
    Height = None
    Length = None
    Width = None
    
    def getSize(self):
        return (self.Width, self.Height, self.Length)
    size = property(getSize, None, None, "Returns the level's dimensions as a tuple (X,Y,Z)")
    
    def compressedSize(self):
        "return the size of the compressed data for this level, in bytes."
        self.compress();
        if self.compressedTag is None: return 0
        return len(self.compressedTag)
        
    def compress(self):
        if self.root_tag is None:
            #print "Asked to compress unloaded chunk! ", self.chunkPosition
            return;
        if self.needsCompression or (self.compressedTag is None and self.root_tag != None):
            #compress if the compressed data is dirty, 
            #or if it's missing, and we also have uncompressed data
            #(if both data are missing, the chunk is not even loaded)
            
            self.packChunkData();
            
            buf = StringIO.StringIO()
            gzipper = gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=2)
            
                
            self.root_tag.save(buf=gzipper)
            gzipper.close();
            
            self.compressedTag = buf.getvalue()
            self.needsCompression = False;
        self.root_tag = None
        
    def decompress(self):
        if self.root_tag != None: return
        if self.compressedTag is None: return
        
        gzipper = gzip.GzipFile(fileobj=StringIO.StringIO(self.compressedTag))
        try:
            data = gzipper.read();
            if data == None: return;
        except IOError:
            data = self.compressedTag
        gzipper.close();

        try:       
            self.root_tag = nbt.load(buf=fromstring(data, dtype='uint8'));
        except (IOError,TypeError):
            print "Malformed NBT data: ", self;
            #self.world.malformedChunk(*self.chunkPosition);
            return;
        
        try:
            self.shapeChunkData()
            #print self.chunks[(x,z)].strides
        except KeyError:
            print "Malformed chunk file: ", self.filename
            #self.world.malformedChunk(*self.chunkPosition);
            return;
        
        self.unpackChunkData();
    
    def packChunkData(self): pass;
    def unpackChunkData(self): pass;
    
    def compressChunk(self, x, z): pass
    def entitiesAt(self, x, y, z):
        return None
    def tileEntitiesAt(self, x, y, z):
        return None
    def addEntity(self, *args): pass
    def addTileEntity(self, *args): pass
    
    def loadChunk(self, x, z):
        pass;
    
    def getPresentChunks(self):
        return itertools.product(xrange(0, self.Width>>4), xrange(0, self.Length>>4))
    presentChunks = property(getPresentChunks)
    
    def getChunk(self, cx, cz):
        #if not hasattr(self, 'whiteLight'):
            #self.whiteLight = array([[[15] * self.Height] * 16] * 16, uint8);
    
        class FakeChunk:
            def load(self):pass
            def compress(self):pass
        
        f = FakeChunk()
        f.Blocks = self.blocksForChunk(cx, cz)
        
        whiteLight = zeros_like(f.Blocks);
        whiteLight[:] = 15;
        
        f.BlockLight = whiteLight
        f.SkyLight = whiteLight
        f.root_tag = TAG_Compound();
        
        return f
        
    def containsPoint(self, x, y, z):
        return (x >=0 and x < self.Width and
                y >=0 and y < self.Height and
                z >=0 and z < self.Length )

    def containsChunk(self, cx, cz):
        #w+15 to allow non 16 aligned schematics
        return (cx >=0 and cx < (self.Width+15 >> 4) and
                cz >=0 and cz < (self.Length+15 >> 4))

    
    def lightsForChunk(self, cx, cz):
        return None;
    def skyLightForChunk(self, cx, cz):
        return None;
    
    def blocksForChunk(self, cx, cz):
        #return a 16x16xH block array for rendering.  Alpha levels can
        #just return the chunk data.  other levels need to reorder the
        #indices and return a slice of the blocks.
        
        cxOff = cx << 4
        czOff = cz << 4
        b = self.Blocks[cxOff:cxOff+16, czOff:czOff+16, 0:self.Height, ];
        #(w, l, h) = b.shape
        #if w<16 or l<16:
        #    b = resize(b, (16,16,h) )
        return b;
    
    def skylightAt(self, *args):
        return 15

    def setSkylightAt(self, *args): pass

    def setBlockDataAt(self, x,y,z, newdata): pass     

    def blockDataAt(self, x, y, z): return 0;
    
    def blockLightAt(self, x, y, z): return 15;

    def blockAt(self, x, y, z):
        if x<0 or y<0 or z<0: return 0
        if x>=self.Width or y>=self.Height or z>=self.Length: return 0;
        return self.Blocks[x,z,y]
    
    

    def blocksInRanges(self, origin, size):
        # origin is (x,y,z), size is (w,h,l)
        (x,y,z) = origin
        (w,h,l) = size
#        end = tuple([o+s for o,s in zip(origin,size)])
        return self.Blocks[x:x+w,z:z+l,y:y+h]
    
    def fillBlocks(self, box, blockType, blockData = 0):
        slices = map(slice, box.origin, box.maximum)
        print slices;
        self.Blocks[slices[0],slices[2],slices[1]] = blockType;
        if hasattr(self, "Data"):
            self.Data[slices[0],slices[2],slices[1]] = blockData;
        
        #self.saveInPlace();
    
    def conversionTableFromLevel(self, level):
        return level.materials.conversionTables[self.materials]
            
    def rotateLeft(self):
        self.root_tag[Blocks].value = swapaxes(self.Blocks, 1, 0)[:,::-1,:]; #x=y; y=-x
    
        
    def copyBlocksFromFiniteToFinite(self, sourceLevel, sourceBox, destinationPoint, copyAir, copyWater):
        # assume destinationPoint is entirely within this level, and the size of sourceBox fits entirely within it.
        sourcex, sourcey, sourcez = map(slice, sourceBox.origin, sourceBox.maximum)
        destCorner2 = map(lambda a,b:a+b, sourceBox.size, destinationPoint)
        destx, desty, destz = map(slice, destinationPoint, destCorner2)
        
        print destx, destz, desty, self.Blocks.shape;
        print sourceBox, sourcex, sourcez, sourcey, sourceLevel.Blocks.shape;
        
        convertedSourceBlocks = self.conversionTableFromLevel(sourceLevel)[sourceLevel.Blocks[sourcex, sourcez, sourcey]]
        self.copyBlockArrayMasked(self.Blocks[destx, destz, desty], convertedSourceBlocks, copyAir, copyWater)
        
        #blocks[:] = convertedSourceBlocks
        
    def copyBlocksFromInfinite(self, sourceLevel, sourceBox, destinationPoint, copyAir, copyWater):
        
        chunkIterator = sourceLevel.getChunkSlices(sourceBox)
        
        for (chunk, slices, point) in chunkIterator:
            point = map(lambda a,b:a+b, point, destinationPoint)
            point = point[0], point[2], point[1]
            #print self.Blocks[ [slice(p, p+s.stop-s.start) for p,s in zip(point,slices) ] ].shape, chunk.Blocks[slices].shape
            convertedSourceBlocks = self.conversionTableFromLevel(sourceLevel)[chunk.Blocks[slices]]
            
            destSlices = [slice(p, p+s.stop-s.start) for p,s in zip(point,slices) ]
            mask = self.copyBlockArrayMasked( self.Blocks[ destSlices ], convertedSourceBlocks, copyAir, copyWater)
            if mask != None:
                self.Data[ destSlices ][mask] = chunk.Data[slices][mask]
            else:
                self.Data[ destSlices ] = chunk.Data[slices]
            
        
        
    def copyBlocksFrom(self, sourceLevel, sourceBox, destinationPoint, copyAir = True, copyWater = True):
        if (not isinstance(sourceLevel, MCInfdevOldLevel)) and not(
               sourceLevel.containsPoint(*sourceBox.origin) and
               sourceLevel.containsPoint(*map(lambda x:x-1, sourceBox.maximum))):
            raise ValueError, "{0} cannot provide blocks between {1}".format(sourceLevel, sourceBox)     
        
        
        # if the destination box is outside the level, it and the source corners are moved inward to fit.
        # ValueError is raised if the source corners are outside sourceLevel
        (x,y,z) = destinationPoint;
        
        
        (lx,ly,lz) = sourceBox.size;
        print "Source: ", sourceLevel
        print "Destination: ", self
        print "Asked to copy {0} blocks from {1} to {2}" .format (ly*lz*lx,sourceBox, destinationPoint)

        #clip the source ranges to this level's edges.  move the destination point as needed.
        if y<0: 
            sourceBox.origin[1] -=y
            sourceBox.size[1] += y
            y = 0;
        if y+sourceBox.size[1]>self.Height:
            sourceBox.size[1] -=y+sourceBox.size[1]-self.Height
            y=self.Height-sourceBox.size[1]
        
        if self.Width != 0:
            if x<0: 
                sourceBox.origin[0] -=x
                sourceBox.size[0] += x
                x = 0;
            if x+sourceBox.size[0]>self.Width:
                sourceBox.size[0] -=x+sourceBox.size[0]-self.Width
                x=self.Width-sourceBox.size[0]
            
        if self.Length != 0:
            if z<0: 
                sourceBox.origin[2] -=z
                sourceBox.size[2] += z
                z = 0;
            if z+sourceBox.size[2]>self.Length:
                sourceBox.size[2] -=z+sourceBox.size[2]-self.Length
                z=self.Length-sourceBox.size[2]
            
        destinationPoint = (x,y,z)
        (lx,ly,lz) = sourceBox.size;
        print "Copying {0} blocks from {1} to {2}" .format (ly*lz*lx,sourceBox, destinationPoint)
       
        if not isinstance(sourceLevel, MCInfdevOldLevel):
            self.copyBlocksFromFiniteToFinite(sourceLevel, sourceBox, destinationPoint, copyAir, copyWater)
        else:
            self.copyBlocksFromInfinite(sourceLevel, sourceBox, destinationPoint, copyAir, copyWater)
        
        
        self.copyEntitiesFrom(sourceLevel, sourceBox, destinationPoint)

    def saveInPlace(self):
        self.saveToFile(self.filename);
    @classmethod
    def fromFile(cls, filename, loadInfinite=True):
        ''' The preferred method for loading Minecraft levels of any type.
        pass False to loadInfinite if you'd rather not load infdev levels.'''
        print "Identifying ", filename
        
        if not filename:
            raise ValueError
        if not os.path.exists(filename):
            raise IOError, "File not found: "+filename
        try:
            f = file(filename,'rb');
        except IOError, e:
            #directory, maybe?
            if not loadInfinite:
                raise;
            try:
                print "Can't read, attempting to open directory"
                lev = MCInfdevOldLevel(filename=filename)
                print "Detected Alpha world."
                return lev;
            except Exception, ex:
                print "Couldn't understand this level: ", e, ex
                raise; 
        rawdata = f.read()
        f.close()
        if len(rawdata) < 4:
            raise ValueError, "File is too small!  " + filename
        
        data = fromstring(rawdata, dtype='uint8')
        isJavaLevel = lambda data: (
            data[0] == 0x27 and
            data[1] == 0x1B and
            data[2] == 0xb7 and
            data[3] == 0x88)
        
        if isJavaLevel(data):
            print "Detected Java-style level"
            lev = MCJavaLevel(data, filename);
            lev.compressed = False;
            return lev;

        #ungzdata = None
        compressed = True
        try:
            data = gzip.GzipFile(fileobj=StringIO.StringIO(rawdata)).read();
        except Exception,e:
            print "Exception during Gzip operation, assuming {0} uncompressed: ".format(filename), e
            compressed = False;
        #if(ungzdata): data=ungzdata
        
        data = fromstring(data, dtype='uint8')
        
        if isJavaLevel(data):
            print "Detected compressed Java-style level"
            lev = MCJavaLevel(data, filename);
            lev.compressed = compressed;
            return lev;

        try:
            root_tag = nbt.load(buf=data);
        except IOError, e:
            print e
            #it must be a plain array of blocks. see if MCJavaLevel handles it.
            print "Detected compressed flat block array, yzx ordered"
            lev = MCJavaLevel(data, filename);
            lev.compressed = compressed;
            return lev;

        else:
            if(root_tag.name == MinecraftLevel):
                print "Detected Indev .mclevel"
                return MCIndevLevel(root_tag, filename)
            if(root_tag.name == "Schematic"):
                print "Detected Schematic."
                return MCSchematic(root_tag=root_tag, filename=filename)
            
            if(root_tag.name == '' and loadInfinite):
                print "Detected Infdev level.dat"
                
                return MCInfdevOldLevel(root_tag=root_tag, filename=filename);

        raise IOError, "Cannot detect file type."
    
    def setPlayerPosition(self, pos):
        pass;

    def playerPosition(self):
        return (8,self.Height*0.75,8);

    def setPlayerSpawnPosition(self, pos):
        pass;

    def playerSpawnPosition(self):
        return self.playerPosition();

    def setPlayerOrientation(self, yp):
        pass

    def playerOrientation(self):
        return (-45.,0.)

    def getEntitiesInRange(self, sourceBox, entities):
        entsInRange = [];
        for entity in entities:
            x,y,z = map(lambda x:x.value, entity[Pos])
            if not (x,y,z) in sourceBox: continue
            entsInRange.append(entity)
            
        #if isinstance(self, MCSchematic): print "Entities ", entities, entsInRange
        return entsInRange
    
    def getTileEntitiesInRange(self, sourceBox, tileEntities):
        entsInRange = [];
        for tileEntity in tileEntities:
            x,y,z = tileEntity['x'].value, tileEntity['y'].value, tileEntity['z'].value  
            if not (x,y,z) in sourceBox: continue
            entsInRange.append(tileEntity)
            
        #if isinstance(self, MCSchematic): print "TileEntities ", tileEntities, entsInRange
        return entsInRange
    
    def copyEntitiesFromInfinite(self, sourceLevel, sourceBox, destinationPoint):
        chunkIterator = sourceLevel.getChunkSlices(sourceBox);
        
        for (chunk, slices, point) in chunkIterator:
            #remember, slices are ordered x,z,y so you can subscript them like so:  chunk.Blocks[slices]
            cx,cz = chunk.chunkPosition
            wx,wz = cx<<4, cz<<4
            
            copyOffset = map(lambda x,y:x-y, destinationPoint, sourceBox.origin)
            for entity in chunk.Entities:
                x,y,z = map(lambda x:int(x.value), entity[Pos])
                if x-wx<slices[0].start or x-wx>=slices[0].stop: continue
                if y<slices[2].start or y>=slices[2].stop: continue
                if z-wz<slices[1].start or z-wz>=slices[1].stop: continue
                
                destX, destZ, destY = copyOffset[0]+x, copyOffset[2]+z, copyOffset[1]+y
                
                eTag = deepcopy(entity)
                #adjust the entity tag's position, making sure to keep its position within the block
                eOffsets = map(lambda pos:pos.value-int(pos.value), eTag[Pos])
                eTag[Pos] = nbt.TAG_List(map(lambda dest, off: nbt.TAG_Double(dest+off), (destX, destY, destZ), eOffsets))
                self.addEntity(eTag);
                
            for tileEntity in chunk.TileEntities:
                x,y,z = tileEntity['x'].value, tileEntity['y'].value, tileEntity['z'].value  
                if x-wx<slices[0].start or x-wx>=slices[0].stop: continue
                if y<slices[2].start or y>=slices[2].stop: continue
                if z-wz<slices[1].start or z-wz>=slices[1].stop: continue
                
                eTag = deepcopy(tileEntity)
                eTag['x'] = TAG_Int(x+copyOffset[0])
                eTag['y'] = TAG_Int(y+copyOffset[1])
                eTag['z'] = TAG_Int(z+copyOffset[2])
                self.addTileEntity(eTag)
                
            
                    
                
    def copyEntitiesFrom(self, sourceLevel, sourceBox, destinationPoint):
        #assume coords have already been adjusted by copyBlocks
        if not self.hasEntities or not sourceLevel.hasEntities: return;
        sourcePoint0 = sourceBox.origin;
        sourcePoint1 = sourceBox.maximum;
        
        if isinstance(sourceLevel, MCInfdevOldLevel):
            self.copyEntitiesFromInfinite(sourceLevel, sourceBox, destinationPoint)
        else:
            entsCopied = 0;
            tileEntsCopied = 0;
            copyOffset = map(lambda x,y:x-y, destinationPoint, sourcePoint0)
            for entity in sourceLevel.getEntitiesInRange(sourceBox, sourceLevel.Entities):
                eTag = deepcopy(entity)
                eOffsets = map(lambda pos:pos.value-int(pos.value), eTag[Pos])
                eTag[Pos] = nbt.TAG_List(map(lambda pos, off, co: nbt.TAG_Double(pos+co+off), map(lambda x:int(x.value), eTag[Pos]), eOffsets, copyOffset))
                self.addEntity(eTag)
                entsCopied += 1;
                
                
            for entity in sourceLevel.getTileEntitiesInRange(sourceBox, sourceLevel.TileEntities):
                x,y,z = entity['x'].value, entity['y'].value, entity['z'].value  
                
                eTag = deepcopy(entity)
                eTag['x'] = TAG_Int(x+copyOffset[0])
                eTag['y'] = TAG_Int(y+copyOffset[1])
                eTag['z'] = TAG_Int(z+copyOffset[2])
                try:
                    self.addTileEntity(eTag)
                    tileEntsCopied += 1;
                except ChunkNotPresent:
                    pass
            print "Copied {0} entities, {1} tile entities".format(entsCopied, tileEntsCopied)
            
            """'''
            copyOffset = map(lambda x,y:x-y, destinationPoint, sourcePoint0)
            if sourceLevel.hasEntities:
                for sx in range(sourcePoint0[0], sourcePoint1[0]):
                    for sy in range(sourcePoint0[1], sourcePoint1[1]):
                        for sz in range(sourcePoint0[2], sourcePoint1[2]):
                            destX, destZ, destY = copyOffset[0]+sx, copyOffset[2]+sz, copyOffset[1]+sy
                            entities = sourceLevel.entitiesAt(sx,sy,sz);
                            tileentities = sourceLevel.tileEntitiesAt(sx,sy,sz);
                            if entities:
                                for eTag in entities:
                                    eTag = deepcopy(eTag)
                                    #adjust the entity tag's position, making sure to keep its position within the block
                                    eOffsets = map(lambda pos:pos.value-int(pos.value), eTag[Pos])
                                    eTag[Pos] = nbt.TAG_List(map(lambda dest, off: nbt.TAG_Double(dest+off), (destX, destY, destZ), eOffsets))
                                    self.addEntity(eTag);
            
                            if tileentities:
                                for eTag in tileentities:
                                    eTag = deepcopy(eTag)
                                    vals = map(lambda dest: nbt.TAG_Int(dest), (destX, destY, destZ))
                                    for i,v in zip('xyz',vals): eTag[i]=v;
                                    self.addTileEntity(eTag);'''"""
                

    def copyBlockArrayMasked(self, blocks, sourceBlocks, copyAir, copyWater):
        #assumes sourceBlocks has already been converted to my materials
        if not copyAir:
            mask=(sourceBlocks!=0)
            if not copyWater:
                mask &=(sourceBlocks != self.materials.materialNamed("Water"))
                mask &=(sourceBlocks != self.materials.materialNamed("Stationary Water"))
                
            blocks[mask] = sourceBlocks[mask]
            return mask;
        
        else:
            blocks[:] = sourceBlocks[:]
        
    def extractSchematic(self, box):
        tempSchematic = MCSchematic(shape=box.size)
        tempSchematic.materials = self.materials
        tempSchematic.copyBlocksFrom(self, box, (0,0,0))   
        return tempSchematic
        
fromFile = MCLevel.fromFile

                
def loadWorldNumber(i):
    filename = "{0}{1}{2}{3}{1}{4}".format(saveFileDir, os.sep, u"World", i,  u"level.dat")
    return fromFile(filename)

##class MCEntity:
##    def __init__(self, tag=None):
##        self.id = "Unknown Entity"
##        
##        if(tag):
##            self.id = tag["id"].value;
##            
##        else:
##            self.id = "Unknown Entity"

class MCSchematic (MCLevel):
    materials = materials
    hasEntities = True;
    
    
    def __str__(self):
        return "MCSchematic(shape={0}, filename=\"{1}\")".format( self.size, self.filename or "")
        
    #these refer to the blocks array instead of the file's height because rotation swaps the axes
    # this will have an impact later on when editing schematics instead of just importing/exporting
    @decompress_first        
    def getLength(self):return self.Blocks.shape[1]
    @decompress_first        
    def getWidth(self):return self.Blocks.shape[0]
    @decompress_first        
    def getHeight(self):return self.Blocks.shape[2]
    
    Length = property(getLength);
    Width = property(getWidth);
    Height = property(getHeight);
    
    
    @decompress_first        
    def getBlocks(self): 
        return self.root_tag[Blocks].value
    Blocks = property(getBlocks);
    
    @decompress_first        
    def getData(self): 
        return self.root_tag[Data].value
    Data = property(getData);
    
    @decompress_first        
    def getHeightMap(self): 
        return self.root_tag[HeightMap].value
    HeightMap = property(getHeightMap);
    
    @decompress_first        
    def getSkyLight(self): 
        return self.root_tag[SkyLight].value
    SkyLight = property(getSkyLight);
    
    @decompress_first        
    def getBlockLight(self): 
        return self.root_tag[BlockLight].value
    BlockLight = property(getBlockLight);
        
    @decompress_first        
    def getEntities(self): 
        return self.root_tag[Entities]
    Entities = property(getEntities);
        
    @decompress_first        
    def getTileEntities(self): 
        return self.root_tag[TileEntities]
    TileEntities = property(getTileEntities);
    
    def __init__(self, shape = None, root_tag = None, filename = None, mats = 'Alpha'):
        """ shape is (x,y,z) for a new level's shape.  if none, takes
        root_tag as a TAG_Compound for an existing schematic file.  if
        none, tries to read the tag from filename.  if none, results
        are undefined. materials can be a MCMaterials instance, or
        "Classic" or "Alpha" to indicate allowable blocks. The default is
        Alpha.

        block coordinate order in the file is y,z,x to use the same code as classic/indev levels.  
        in hindsight, this was a completely arbitrary decision.
        
        the Entities and TileEntities are nbt.TAG_List objects containing TAG_Compounds.
        this makes it easy to copy entities without knowing about their insides.
        
        rotateLeft swaps the axes of the different arrays.  because of this, the Width, Height, and Length
        reflect the current dimensions of the schematic rather than the ones specified in the NBT structure.
        I'm not sure what happens when I try to re-save a rotated schematic.
        """

        #if(shape != None):
        #    self.setShape(shape)
        
        
        if filename:
            self.filename = filename
            if None is root_tag:
                try:
                    root_tag = nbt.load(filename)
                except IOError,e:
                    print "Failed to load file ", e
                    
        else:
            self.filename = None

        if mats in namedMaterials:
            self.materials = namedMaterials[mats];
        else:
            assert(isinstance(materials, MCMaterials))
            self.materials = mats
 
        if root_tag:
            #self.Entities = root_tag[Entities];
            #self.TileEntities = root_tag[TileEntities];
               
            if Materials in root_tag:
                self.materials = namedMaterials[root_tag[Materials].value]
            self.root_tag = root_tag;
            self.shapeChunkData();
            
        else:
            assert shape != None
            root_tag = TAG_Compound(name="Schematic")
            root_tag[Height] = TAG_Int(shape[1])
            root_tag[Length] = TAG_Int(shape[2])
            root_tag[Width] = TAG_Int(shape[0])
            
            root_tag[Entities] = TAG_List()
            root_tag[TileEntities] = TAG_List()
            root_tag["Materials"] = TAG_String(materialNames[self.materials]);
            
            root_tag[Blocks] = TAG_Byte_Array( zeros( (shape[1], shape[2], shape[0]), uint8 ) )
            root_tag[Data] = TAG_Byte_Array( zeros( (shape[1], shape[2], shape[0]), uint8 ) )
            
            self.root_tag = root_tag;
        
        self.unpackChunkData();
            
    def shapeChunkData(self):
        w = self.root_tag[Width].value
        l = self.root_tag[Length].value
        h = self.root_tag[Height].value
        
        self.root_tag[Blocks].value.shape=(h,l,w)
        self.root_tag[Data].value.shape=(h,l,w)
       
    def packChunkData(self):
        self.root_tag[Blocks].value = swapaxes(self.root_tag[Blocks].value, 0, 2)#yzx to xzy
        self.root_tag[Data].value = swapaxes(self.root_tag[Data].value, 0, 2)#yzx to xzy
        
    def unpackChunkData(self):
        self.packChunkData();
        
    def rotateLeft(self):
        MCLevel.rotateLeft(self);
        
        self.Data = swapaxes(self.Data, 1, 0)[:,::-1,:]; #x=z; z=-x
        
        torchRotation = array([0, 4, 3, 1, 2, 5,
                               6, 7, 
                               
                               8, 9, 10, 11, 12, 13, 14, 15]);
                               
        torchIndexes = (self.Blocks == self.materials.materialNamed("Torch"))
        if self.materials == materials:
            torchIndexes |= ( (self.Blocks == self.materials.materialNamed("Redstone Torch (on)")) | 
                              (self.Blocks == self.materials.materialNamed("Redstone Torch (off)")) )
                              
        print "Rotating torches: ", len(torchIndexes.nonzero()[0]);
        self.Data[torchIndexes] = torchRotation[self.Data[torchIndexes]]
        
        
        if self.materials == materials:
            railRotation = array([1, 0, 4, 5, 3, 2, 9, 6, 
                                   7, 8, 
                                   
                                   10, 11, 12, 13, 14, 15]);
                                   
            railIndexes = (self.Blocks == self.materials.materialNamed("Rail"))
            print "Rotating rails: ", len(railIndexes.nonzero()[0]);
            self.Data[railIndexes] = railRotation[self.Data[railIndexes]]
            
            
            
            
            ladderRotation = array([0, 1, 4, 5, 3, 2, 
                
                                   6, 7,  #xxx more ladders
                                   8, 9, 10, 11, 12, 13, 14, 15]);
                                   
            ladderIndexes = (self.Blocks == self.materials.materialNamed("Ladder"))
            print "Rotating ladders: ", len(ladderIndexes.nonzero()[0]);
            self.Data[ladderIndexes] = ladderRotation[self.Data[ladderIndexes]]
            
            signIndexes = (self.Blocks == self.materials.materialNamed("Sign"))
            print "Rotating signs: ", len(signIndexes.nonzero()[0]);
            self.Data[signIndexes] -= 4
            self.Data[signIndexes] &= 0xf
            
            wallSignRotation = array([0, 1, 4, 5, 3, 2, 6, 7, 
                                      8, 9, 10, 11, 12, 13, 14, 15]);
            
            wallSignIndexes = (self.Blocks == self.materials.materialNamed("Wall Sign"))
            print "Rotating wallsigns: ", len(wallSignIndexes.nonzero()[0]);
            self.Data[wallSignIndexes] = wallSignRotation[self.Data[wallSignIndexes]]
            
            
        print "Relocating entities..."
        for entity in self.Entities:
            for p in "Pos", "Motion":
                newX = entity[p][2].value
                newZ = self.Length - entity[p][0].value
                
                entity[p][0].value = newX
                entity[p][2].value = newZ
            entity["Rotation"][0].value += 90.0
        
        for tileEntity in self.TileEntities:
            newX = tileEntity["z"].value
            newZ = self.Length - tileEntity["x"].value
            
            tileEntity["x"].value = newX
            tileEntity["z"].value = newZ
            
    @decompress_first
    def setShape(self, shape):
        """shape is a tuple of (width, height, length).  sets the
        schematic's properties and clears the block and data arrays"""

        x, y, z = shape
        shape = (x,z,y)
        
        
        self.root_tag[Blocks].value = zeros(dtype='uint8',shape=shape)
        self.root_tag[Data].value = zeros(dtype='uint8',shape=shape)
        self.shapeChunkData();
        
    def saveToFile(self, filename = None):
        """ save to file named filename, or use self.filename.  XXX NOT THREAD SAFE AT ALL. """
        if filename == None: filename = self.filename
        if filename == None:
            print "Attempted to save an unnamed schematic in place :x"
            return; #you fool!

        #root_tag = nbt.TAG_Compound(name="Schematic")
        #root_tag[Height] = nbt.TAG_Short(self.Height)
        #root_tag[Length] = nbt.TAG_Short(self.Length)
        #root_tag[Width] = nbt.TAG_Short(self.Width)
        #root_tag[Blocks] = nbt.TAG_Byte_Array(swapaxes(self.Blocks.reshape(self.Width,self.Length,self.Height), 0, 2)) #xxx hardcoded
        #root_tag[Data] = nbt.TAG_Byte_Array(swapaxes(self.Data.reshape(self.Width,self.Length,self.Height), 0, 2))
        #root_tag[Entities] = self.Entities;
        #root_tag[TileEntities] = self.TileEntities;
        #root_tag[Materials] = nbt.TAG_String(materialNames[self.materials])
        #self.packChunkData();
        self.compress();
        chunkfh = file(filename, 'wb')
        chunkfh.write(self.compressedTag)
        chunkfh.close()
        
        #self.root_tag.saveGzipped(filename);
        #self.unpackChunkData();
        

    def setBlockDataAt(self, x,y,z, newdata):
        if x<0 or y<0 or z<0: return 0
        if x>=self.Width or y>=self.Height or z>=self.Length: return 0;
        self.Data[x,z,y] |= (newdata & 0xf) << 4;        

    def blockDataAt(self, x, y, z):
        if x<0 or y<0 or z<0: return 0
        if x>=self.Width or y>=self.Height or z>=self.Length: return 0;
        return (self.Data[x,z,y] & 0xf0) >> 4;

    def entitiesAt(self, x, y, z):
        entities = [];
        for entityTag in self.Entities:
            if map(lambda x:int(x.value), entityTag[Pos]) == [x,y,z]:
                entities.append(entityTag);
        
        return entities;

    def addEntity(self, entityTag):
        assert isinstance(entityTag, TAG_Compound)
        self.Entities.append(entityTag);
        
    def tileEntitiesAt(self, x, y, z):
        entities = [];
        for entityTag in self.TileEntities:
            pos = [entityTag[a].value for a in 'xyz']
            if pos == [x,y,z]:
                entities.append(entityTag);

        return entities;

    def addTileEntity(self, entityTag):
        assert isinstance(entityTag, TAG_Compound)
        self.TileEntities.append(entityTag);
    
class ChunkNotPresent(Exception): pass
class ChunkMalformed(ChunkNotPresent): pass

class ZeroChunk:
    " a placebo for neighboring-chunk routines "
    def compress(self): pass
    def load(self): pass
    def __init__(self, height=256):
        zeroChunk = zeros((16,16,height), uint8)
    
        self.Blocks = zeroChunk
        self.BlockLight = zeroChunk
        self.SkyLight = zeroChunk
        self.Data = zeroChunk
        HeightMap = zeros((16,16),uint8)
            
    
class InfdevChunk(MCLevel):
    """ This is a 16,16,128 chunk in an (infinite) world.
    The properties Blocks, Data, SkyLight, BlockLight, and Heightmap 
    are ndarrays containing the respective blocks in the chunk file.
    Each array is indexed [x,z,y].  The Data, Skylight, and BlockLight 
    arrays are automatically unpacked from nibble arrays into byte arrays 
    for better handling.
    """
    def __init__(self, world, chunkPosition, create = False):
        self.world = world;
        self.chunkPosition = chunkPosition;
        self.filename = world.chunkFilename(*chunkPosition);
        self.compressedTag = None
        self.root_tag = None
        self.dirty = False;
        self.needsCompression = False;
        self.needsLighting = False
        
        if create:
            self.create();
            
    def __str__(self):
        return "InfdevChunk, coords:{0}, world: {1}, D:{2}, C:{3}, L:{4}".format(self.chunkPosition, os.path.split(self.world.worldDir)[1],self.dirty, self.needsCompression, self.needsLighting)

    def create(self):
        (cx,cz) = self.chunkPosition;
        chunkTag = nbt.TAG_Compound()
        chunkTag.name = ""
        levelTag = nbt.TAG_Compound()
        chunkTag[Level] = levelTag
        
        levelTag[TerrainPopulated] = TAG_Byte(1)
        levelTag[xPos] = TAG_Int(cx)
        levelTag[zPos] = TAG_Int(cz)
        
        levelTag[LastUpdate] = TAG_Int(0);
        
        levelTag[BlockLight] = TAG_Byte_Array()
        levelTag[BlockLight].value = zeros(16*16*64, uint8)
        
        levelTag[Blocks] = TAG_Byte_Array()
        levelTag[Blocks].value = zeros(16*16*128, uint8)
        
        levelTag[Data] = TAG_Byte_Array()
        levelTag[Data].value = zeros(16*16*64, uint8)

        levelTag[SkyLight] = TAG_Byte_Array()
        levelTag[SkyLight].value = zeros(16*16*64, uint8)

        levelTag[HeightMap] = TAG_Byte_Array()
        levelTag[HeightMap].value = zeros(16*16, uint8)

        levelTag[Entities] = TAG_List() 
        levelTag[TileEntities] = TAG_List()
        
        #levelTag["Creator"] = TAG_String("MCEdit-" + release.release);
        
        #empty lists are seen in the wild with a list.TAG_type for a list of single bytes, 
        #even though these contain TAG_Compounds 
        
        self.root_tag = chunkTag
        self.shapeChunkData();
        self.unpackChunkData();
        dx = os.path.join(self.world.worldDir, self.world.dirhash(cx))
        dz = os.path.join(dx, self.world.dirhash(cz))
        
                
        try:
            os.mkdir(dx)
        except Exception, e: 
            #print "Failed to make chunk dir x ", dx, e
            pass
        try:
            os.mkdir(dz)
        except: 
            #print "Failed to make chunk dir z ", dz, e
            pass
        
        self.save();
    
    def remove(self):
        os.remove(self.filename)
        self.root_tag = None
                
    def save(self):
        """ does not recalculate any data or light """
        #print "Saving chunk: ", (cx, cz), self._presentChunks[(cx,cz)]
        self.compress()
        
        if self.dirty:
            #atomic operation:  move old file out of the way?  no, do it better
            try:
                os.rename(self.filename, self.filename + ".old")
            except Exception,e:
                #print "No existing chunk file to rename"
                pass
            try:
                chunkfh = file(self.filename, 'wb')
                chunkfh.write(self.compressedTag)
                chunkfh.close()
                
                #print "Saved chunk ", self._presentChunks[(cx,cz)];
            except IOError,e:
                try: os.rename(self.filename + ".old", self.filename)
                except: print "Unable to restore old chunk file"
                print "Failed to save ", self.filename, e
                
            try: os.remove(self.filename + ".old")
            except Exception,e:
                #print "No old chunk file to remove"
                pass
            #print "Saved chunk ", self._presentChunks[(cx,cz)];
            self.dirty = False;
            
    def load(self):
        if self.compressedTag is None:
            compressedData = file(self.filename, 'rb')
            self.compressedTag = compressedData.read();
            compressedData.close()

        if self.root_tag is None:
            self.decompress()
    
    def isLoaded(self):
        #we're loaded if we have our tag data in ram 
        #and we don't have to go back to the disk for it.
        return not (self.compressedTag is None and self.root_tag is None)
            
        
    def chunkChanged(self, calcLighting = True):
        if self.root_tag != None:
            self.needsCompression = True;
            
        elif self.compressedTag == None:
            #unloaded chunk
            return;
            
        self.dirty = True;
        self.needsLighting = True;
        self.generateHeightMap();
        if calcLighting:
            self.genFastLights()
            
    def ready(self):
        return not (self.compressedTag is None)
    
    def isCompressed(self):
        return self.compressedTag != None and self.root_tag == None
    
    def genFastLights(self):
        (cx,cz) = self.chunkPosition
            
        self.SkyLight[:] = 0;
        for x,z in itertools.product(xrange(16), xrange(16)):
            
            self.SkyLight[x,z,self.HeightMap[z,x]:128] = 15 
            lv = 15;
            for y in xrange(self.HeightMap[z,x]).__reversed__():
                lv -= max(la[self.Blocks[x,z,y]], 1)
                
                if lv <= 0: 
                    break;
                self.SkyLight[x,z,y] = lv;
                
    def generateHeightMap(self):
        if None is self.root_tag: self.load();
        
        blocks = self.Blocks
        heightMap = self.HeightMap
        heightMap[:] = 0;
        
        lightAbsorption = self.world.materials.lightAbsorption[blocks]
        axes = lightAbsorption.nonzero()
        heightMap[axes[1],axes[0]] = axes[2]; #assumes the y-indices come out in increasing order
        heightMap += 1;
        """
        for (x,z) in itertools.product(range(16), range(16)):
            lv = 15;
            for y in range(self.world.Height).__reversed__():
                la = lightAbsorption[blocks[x,z,y]]
                #if la == 15:
                if la: #xxxx work on heightmap
                    #again, reversed heightmap indices.
                    #we actually need y+1 here  - at least that's how it is in game-genned levels.
                    heightMap[z,x] = y+1; 
                    break;
                lv -= la;
                if lv<=0: 
                    heightMap[z,x] = y+1; 
                    break;  """                 
    
    def unpackChunkData(self):
        """ for internal use.  call getChunk and compressChunk to load, compress, and unpack chunks automatically """
        for key in (SkyLight, BlockLight, Data):
            dataArray = self.root_tag[Level][key].value
            assert dataArray.shape[2] == 64;
            unpackedData = insert(dataArray[...,newaxis], 0, 0, 3)  
            
            #unpack data
            unpackedData[...,0] = unpackedData[...,1]&0xf
            unpackedData[...,1] >>=4  
            #unpackedData[...,1] &= 0x0f   
            
            
            self.root_tag[Level][key].value=unpackedData.reshape(16,16,128)
         
    def packChunkData(self):
        if self.root_tag is None:
            #print "packChunkData called on unloaded chunk! ", self.chunkPosition
            return;
        for key in (SkyLight, BlockLight, Data):
            dataArray = self.root_tag[Level][key].value
            assert dataArray.shape[2] == 128;
            
            unpackedData = self.root_tag[Level][key].value.reshape(16,16,64,2)
            unpackedData[...,1] <<=4
            unpackedData[...,1] |= unpackedData[...,0]
            self.root_tag[Level][key].value=array(unpackedData[:,:,:,1])
            
        
    def shapeChunkData(self):
        """Applies the chunk shape to all of the data arrays 
        in the chunk tag.  used by chunk creation and loading"""
        chunkTag = self.root_tag
        
        chunkSize = 16
        chunkTag[Level][Blocks].value.shape=(chunkSize, chunkSize, 128)
        chunkTag[Level][HeightMap].value.shape=(chunkSize, chunkSize);            
        chunkTag[Level][SkyLight].value.shape = (chunkSize, chunkSize, 64)
        chunkTag[Level][BlockLight].value.shape = (chunkSize, chunkSize, 64)
        chunkTag[Level]["Data"].value.shape = (chunkSize, chunkSize, 64)
        if not TileEntities in chunkTag[Level]:
            chunkTag[Level][TileEntities] = TAG_List();
        if not Entities in chunkTag[Level]:
            chunkTag[Level][Entities] = TAG_List();
    
    @decompress_first        
    def getBlocks(self): 
        return self.root_tag[Level][Blocks].value
    Blocks = property(getBlocks);
    
    @decompress_first        
    def getData(self): 
        return self.root_tag[Level][Data].value
    Data = property(getData);
    
    @decompress_first        
    def getHeightMap(self): 
        return self.root_tag[Level][HeightMap].value
    HeightMap = property(getHeightMap);
    
    @decompress_first        
    def getSkyLight(self): 
        return self.root_tag[Level][SkyLight].value
    SkyLight = property(getSkyLight);
    
    @decompress_first        
    def getBlockLight(self): 
        return self.root_tag[Level][BlockLight].value
    BlockLight = property(getBlockLight);
        
    @decompress_first        
    def getEntities(self): 
        return self.root_tag[Level][Entities]
    Entities = property(getEntities);
        
    @decompress_first        
    def getTileEntities(self): 
        return self.root_tag[Level][TileEntities]
    TileEntities = property(getTileEntities);
    

class MCInfdevOldLevel(MCLevel):
    materials = materials;
    hasEntities = True;
    
    
    def __str__(self):
        return "MCInfdevOldLevel(" + os.path.split(self.worldDir)[1] + ")"
    
    def __init__(self, filename = None, root_tag = None):
        #pass level.dat's root tag and filename to read an existing level.
        #pass only filename to create a new one
        #filename should be the path to the world dir
        self.Length = 0
        self.Width = 0
        self.Height = 128 #subject to change?
        
        if (not (os.sep in filename)) or (os.path.split(filename) and os.path.split(filename)[1].lower() != "level.dat"): #we've been passed a world subdir by some rascal
            self.worldDir = filename
            filename = os.path.join(filename, "level.dat")
        else:
            self.worldDir = os.path.split(filename)[0]
            
        self._presentChunks = {};
        
        if root_tag is None:
            
            if filename == None:
                raise ValueError, "Can't create an Infinite level without a filename!"
            #create a new level
            root_tag = TAG_Compound();
            root_tag[Data] = TAG_Compound();
            root_tag[Data][SpawnX] = TAG_Int(0)
            root_tag[Data][SpawnY] = TAG_Int(2)
            root_tag[Data][SpawnZ] = TAG_Int(0)
            
            root_tag[Data]['LastPlayed'] = TAG_Long(long(time.time()))
            root_tag[Data]['RandomSeed'] = TAG_Long(int(random.random() * ((2<<31))))
            root_tag[Data]['SizeOnDisk'] = TAG_Long(long(1048576))
            root_tag[Data]['Time'] = TAG_Long(1)
            root_tag[Data]['SnowCovered'] = TAG_Byte(0);
            
            ### if singleplayer:
            root_tag[Data][Player] = TAG_Compound()
            
            
            root_tag[Data][Player]['Air'] = TAG_Short(300);
            root_tag[Data][Player]['AttackTime'] = TAG_Short(0)
            root_tag[Data][Player]['DeathTime'] = TAG_Short(0);
            root_tag[Data][Player]['Fire'] = TAG_Short(-20);
            root_tag[Data][Player]['Health'] = TAG_Short(20);
            root_tag[Data][Player]['HurtTime'] = TAG_Short(0);
            root_tag[Data][Player]['Score'] = TAG_Int(0);
            root_tag[Data][Player]['FallDistance'] = TAG_Float(0)
            root_tag[Data][Player]['OnGround'] = TAG_Byte(0)

            root_tag[Data][Player]['Inventory'] = TAG_List()

            root_tag[Data][Player]['Motion'] = TAG_List([TAG_Double(0) for i in range(3)])
            root_tag[Data][Player]['Pos'] = TAG_List([TAG_Double([0.5,2.8,0.5][i]) for i in range(3)])
            root_tag[Data][Player]['Rotation'] = TAG_List([TAG_Float(0), TAG_Float(0)])
            
            #root_tag["Creator"] = TAG_String("MCEdit-"+release.release);
            
            if not os.path.exists(self.worldDir):
                os.mkdir(self.worldDir)
        
         
        self.root_tag = root_tag;
        self.filename = filename;
        
        self.saveInPlace();
        
        self.dirhashes = [self.dirhash(n) for n in range(64)];
        self.dirhash=self.dirhashlookup;

        
        self.preloadChunkPaths();
    
    def preloadChunkPaths(self):
        worldDirs = os.listdir(self.worldDir);
        for dirname in worldDirs :
            if(dirname in self.dirhashes):
                subdirs = os.listdir(os.path.join(self.worldDir, dirname));
                for subdirname in subdirs:
                    if(subdirname in self.dirhashes):
                        filenames = os.listdir(os.path.join(self.worldDir, dirname, subdirname));
                        #def fullname(filename):
                            #return os.path.join(self.worldDir, dirname, subdirname, filename);
                        
                        #fullpaths = map(fullname, filenames);
                        bits = map(lambda x:x.split('.'), filenames);

                        bits = filter(lambda x:(len(x) == 4 and x[0].lower() == 'c' and x[3].lower() == 'dat'), bits)
                        chunks = [(self.decbase36(b[1]), self.decbase36(b[2])) for b in bits if len(b) > 3]
                        
                        for c in chunks:
                            self._presentChunks[c] = InfdevChunk(self, c);
                            
                            #self._presentChunks.update(dict(zip(chunks, fullpaths)));
##                        for filename, chunk in zip(fullpaths, chunks):
##                            chunkfh = file(filename, 'rb')
##                            self.compressedTags[chunk] = chunkfh.read();
##                            chunkfh.close();
                            

##        for chunk in self._presentChunks.keys():
##            self.loadChunk(*chunk);
                        
    def preloadInitialChunks(self, chunks):
        #intended to be called on a second thread.
        #as a side effect, the operating system will have the given chunk files in the file cache.
        for c in chunks:
            chunkfh = file(self._presentChunks[c].filename, 'rb')
            self.compressedTags[c] = chunkfh.read();
            chunkfh.close();
    
    def compressChunk(self, x, z):
        if not (x,z) in self._presentChunks: return; #not an error
        self._presentChunks[x,z].compress()
        
    def discardAllChunks(self):
        """ clear lots of memory, fast. """
        
    def chunkFilenameAt(self, x, y, z):
        cx = x >> 4
        cz = z >> 4
        return self._presentChunks.get( (cx, cz) ).filename
    
    base36alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    def decbase36(self, s):
        n = 0;
        neg = False;
        if s[0] == '-':
            neg = True;
            s=s[1:];
        
        while(len(s)):
            if not s[0] in self.base36alphabet:
                print "Bad letter", s[0];
                break;
            n*=36
            n+=self.base36alphabet.index(s[0])
            s=s[1:];

        if neg: return -n
        return n;
    
    def base36(self, n):
        n = int(n);
        if 0 == n: return '0'
        s = "";
        neg = "";
        if n < 0:
            neg = "-"
            n = -n;
            
        while(n):
            digit = n % 36;
            n /= 36
            s=self.base36alphabet[digit]+s
        
        return neg + s;

    def dirhashlookup(self, n):
        return self.dirhashes[n%64];
        
    def dirhash(self, n):
        n=n%64;
        s="";
        if(n>=36):
            s+="1";
            n-=36;
        s+="0123456789abcdefghijklmnopqrstuvwxyz"[n]

        return s;

    
    def chunkFilename(self, x, z):
        s= os.path.join(self.worldDir, self.dirhash(x), self.dirhash(z),
                                     "c.%s.%s.dat" % (self.base36(x), self.base36(z)));
        return s;
                 
    def chunkFilepath(self, cx, cz):
        return self.chunkFilename(cx,cz)
        #return os.path.join( self.worldDir, self.chunkFilename(cx, cz) )
    
    def blocksForChunk(self, cx, cz):
        return self.getChunk(cx, cz).Blocks;
        
    def lightsForChunk(self, cx, cz):
        return self.getChunk(cx, cz).BlockLight;

    def heightMapForChunk(self, cx, cz):
        return self.getChunk(cx, cz).HeightMap;
    
    def skyLightForChunk(self, cx, cz):
        return self.getChunk(cx, cz).SkyLight;
    
    def blockDataForChunk(self, cx, cz):
        return self.getChunk(cx, cz).Data;
    
        
    def blockLightAt(self, x, y, z):
        if y < 0 or y >= self.Height: return 0
        zc=z >> 4
        xc=x >> 4
        
        xInChunk = x&0xf;
        zInChunk = z&0xf;
        return self.lightsForChunk(xc,zc)[xInChunk,zInChunk,y]
        
        
    def setBlockLightAt(self, x, y, z, newlight):
        if y < 0 or y >= self.Height: return 0
        zc=z>>4
        xc=x>>4
        
        xInChunk = x&0xf;
        zInChunk = z&0xf;
        
        self.lightsForChunk(xc,zc)[xInChunk,zInChunk,y] = newLight

    def blockDataAt(self, x, y, z):
        if y < 0 or y >= self.Height: return 0
        zc=z>>4
        xc=x>>4
        
        xInChunk = x&0xf;
        zInChunk = z&0xf;
        
        return self.blockDataForChunk(xc,zc)[xInChunk,zInChunk,y]

        
    def setBlockDataAt(self, x,y,z, newdata):
        if y < 0 or y >= self.Height: return 0
        zc=z>>4
        xc=x>>4
        

        xInChunk = x&0xf;
        zInChunk = z&0xf;

        
        self.blockDataForChunk(xc,zc)[xInChunk, zInChunk, y] = newdata
        
    def blockAt(self, x, y, z):
        """returns 0 for blocks outside the loadable chunks.  automatically loads chunks."""
        if y < 0 or y >= self.Height: return 0

        zc=z>>4
        xc=x>>4
        xInChunk = x & 0xf;
        zInChunk = z & 0xf;

        return self.blocksForChunk(xc,zc)[xInChunk, zInChunk, y]

    def skylightAt(self, x, y, z):

        if y < 0 or y >= self.Height: return 0
        zc=z>>4
        xc=x>>4
        

        xInChunk = x & 0xf;
        zInChunk = z & 0xf

        return self.skyLightForChunk(xc,zc)[xInChunk, zInChunk, y]

        
    def setSkylightAt(self, x, y, z, lightValue):
        if y < 0 or y >= self.Height: return 0
        zc=z>>4
        xc=x>>4
        
        xInChunk = x & 0xf;
        zInChunk = z & 0xf;

        skyLight = self.skyLightForChunk(xc,zc)
        
        oldValue = skyLight[xInChunk, zInChunk, y]
            
        if oldValue < lightValue: skyLight[xInChunk, zInChunk, y] = lightValue
        return oldValue < lightValue
    
    def heightMapAt(self, x, z):
        zc=z>>4
        xc=x>>4
        
        heightMap = self.heightMapForChunk(xc,zc)
        return heightMap[z&0xf][x&0xf]; 
        #the heightmap is ordered differently because in minecraft it is a flat array
    
    def getPresentChunks(self):
        return self._presentChunks.keys();
    presentChunks = property (getPresentChunks)
    
    def getChunks(self, chunks = None):
        """ pass a list of chunk coordinate tuples to get a list of InfdevChunks. 
        pass nothing for a list of every chunk in the level. 
        the chunks are automatically loaded."""
        if chunks is None: chunks = self.getPresentChunks();
        return [self.getChunk(cx,cz) for (cx,cz) in chunks if (cx,cz) in self._presentChunks]
            
        
    def getChunk(self, cx, cz):
        """ read the chunk from disk, load it, decompress it, unpack its 4-bit arrays to 8-bit, and return it. """
        
        if not (cx,cz) in self._presentChunks: 
            raise ChunkNotPresent, "Chunk {0} not present".format((cx,cz))
        c = self._presentChunks[cx,cz]
        c.load();
        if not (cx,cz) in self._presentChunks:
            raise ChunkMalformed, "Chunk {0} malformed".format((cx,cz))
            
        return c;
        
    def chunkIsCompressed(self, cx, cz):
        if not (cx,cz) in self._presentChunks: raise ChunkNotPresent;
        return self._presentChunks[cx,cz].isCompressed();
        
    def markDirtyChunk(self, cx, cz):
        if not (cx,cz) in self._presentChunks: return
        self._presentChunks[cx,cz].chunkChanged();
    
    def saveInPlace(self):
        dirtyChunkCount = 0;
        for chunk in self._presentChunks.values():
            if chunk.dirty: 
                dirtyChunkCount += 1;
            chunk.save();
            

        self.root_tag.save(self.filename);
        print "Saved {0} chunks".format(dirtyChunkCount);
       
    def generateLights(self, dirtyChunks = None):
        """ dirtyChunks may be an iterable yielding (xPos,zPos) tuples """
        activeBlocks = set();
        #doneBlocks = set()
        la = array(self.materials.lightAbsorption)
                
        startTime = datetime.now();
        print "Initializing lights..."
        if dirtyChunks is None:
            dirtyChunks = filter(lambda x: x.needsLighting, self._presentChunks.values());
        else:
            dirtyChunks = [self._presentChunks[c] for c in dirtyChunks if c in self._presentChunks];
            #[d.genFastLights() for d in dirtyChunks]
        dirtyChunks = sorted(dirtyChunks, key=lambda x:x.chunkPosition) 
        
        for chunk in dirtyChunks:
            chunk.load();
            chunk.chunkChanged();
            #print chunk;
            assert chunk.dirty and chunk.needsCompression and chunk.needsLighting
            #chunk.SkyLight[:] = 0
            #chunk.BlockLight[:] = 0
            chunk.BlockLight[:] = self.materials.lightEmission[chunk.Blocks];
            
        zeroChunk = ZeroChunk(128)
        
        
        #print "Lighting {0} chunks...".format( len(dirtyChunks) )
        #for chunk in dirtyChunks:
            
        la[18] = 0; #for normal light dispersal, leaves absorb the same as empty air.
        
        print "Dispersing light..."
        for light in ("BlockLight", "SkyLight"):
          print light;
          zerochunkLight = getattr(zeroChunk, light); 
          for i in range(16):
            print "Pass ", i
            """
            propagate light!
            for each of the six cardinal directions, figure a new light value for 
            adjoining blocks by reducing this chunk's light by light absorption and fall off. 
            compare this new light value against the old light value and update with the maximum.
            
            we calculate all chunks one step before moving to the next step, to ensure all gaps at chunk edges are filled.  
            we do an extra cycle because lights sent across edges may lag by one cycle.
            """
            for chunk in dirtyChunks:
                #xxx code duplication
                (cx,cz) = chunk.chunkPosition
                neighboringChunks = {};
                for dir,dx,dy,dz in ( (FaceXDecreasing,-1,0,0), 
                                      (FaceXIncreasing,1,0,0), 
                                      (FaceZDecreasing,0,0, -1), 
                                      (FaceZIncreasing,0,0, 1) ):
                   if not self.containsChunk(cx+dx,cz+dz):
                       neighboringChunks[dir] = zeroChunk
                   else:
                       neighboringChunks[dir] = self.getChunk(cx+dx,cz+dz)
                       assert neighboringChunks[dir].root_tag != None
                
                chunkLa = la[chunk.Blocks]+1;
                chunkLight = getattr(chunk,light);
                
                nc = neighboringChunks[FaceXDecreasing]
                ncLight = getattr(nc,light);
                
                #left edge
                newlight = (chunkLight[0:1,:,:128]-la[nc.Blocks[15:16,:,0:128]])-1
                newlight[newlight>15]=0;
                
                ncLight[15:16,:,0:128] = maximum(ncLight[15:16,:,0:128], newlight)
                
                #chunk body
                newlight = (chunkLight[1:16,:,0:128]-chunkLa[0:15,:,0:128])
                newlight[newlight>15]=0; #light went negative;
                
                chunkLight[0:15,:,0:128] = maximum(chunkLight[0:15,:,0:128], newlight)
                
                #right edge
                nc = neighboringChunks[FaceXIncreasing]
                ncLight = getattr(nc,light);
                
                newlight = ncLight[0:1,:,:128]-chunkLa[15:16,:,0:128]
                newlight[newlight>15]=0;
                
                chunkLight[15:16,:,0:128] = maximum(chunkLight[15:16,:,0:128], newlight)
                
                
            
                #right edge
                nc = neighboringChunks[FaceXIncreasing]
                ncLight = getattr(nc,light);
                
                newlight = (chunkLight[15:16,:,0:128]-la[nc.Blocks[0:1,:,0:128]])-1
                newlight[newlight>15]=0;
                
                ncLight[0:1,:,0:128] = maximum(ncLight[0:1,:,0:128], newlight)
                
                #chunk body
                newlight = (chunkLight[0:15,:,0:128]-chunkLa[1:16,:,0:128])
                newlight[newlight>15]=0;
                
                chunkLight[1:16,:,0:128] = maximum(chunkLight[1:16,:,0:128], newlight)
                
                #left edge
                nc = neighboringChunks[FaceXDecreasing]
                ncLight = getattr(nc,light);
                
                newlight = ncLight[15:16,:,:128]-chunkLa[0:1,:,0:128]
                newlight[newlight>15]=0;
                
                chunkLight[0:1,:,0:128] = maximum(chunkLight[0:1,:,0:128], newlight)
               
                zerochunkLight[:] = 0;
                
                
                #bottom edge
                nc = neighboringChunks[FaceZDecreasing]
                ncLight = getattr(nc,light);
                
                newlight = (chunkLight[:,0:1,:128]-la[nc.Blocks[:,15:16,:128]])-1
                newlight[newlight>15]=0;
                
                ncLight[:,15:16,:128] = maximum(ncLight[:,15:16,:128], newlight)
                
                #chunk body
                newlight = (chunkLight[:,1:16,:128]-chunkLa[:,0:15,:128])
                newlight[newlight>15]=0;
                
                chunkLight[:,0:15,:128] = maximum(chunkLight[:,0:15,:128], newlight)
                
                #top edge
                nc = neighboringChunks[FaceZIncreasing]
                ncLight = getattr(nc,light);
                
                newlight = ncLight[:,0:1,:128]-chunkLa[:,15:16,0:128]
                newlight[newlight>15]=0;
                
                chunkLight[:,15:16,0:128] = maximum(chunkLight[:,15:16,0:128], newlight)
               
                   
                #top edge  
                nc = neighboringChunks[FaceZIncreasing]
                
                ncLight = getattr(nc,light);
                
                newlight = (chunkLight[:,15:16,:128]-la[nc.Blocks[:,0:1,:128]])-1
                newlight[newlight>15]=0;
                
                ncLight[:,0:1,:128] = maximum(ncLight[:,0:1,:128], newlight)
                
                #chunk body
                newlight = (chunkLight[:,0:15,:128]-chunkLa[:,1:16,:128])
                newlight[newlight>15]=0;
                
                chunkLight[:,1:16,:128] = maximum(chunkLight[:,1:16,:128], newlight)
                
                #bottom edge
                nc = neighboringChunks[FaceZDecreasing]
                ncLight = getattr(nc,light);
               
                newlight = ncLight[:,15:16,:128]-chunkLa[:,0:1,0:128]
                newlight[newlight>15]=0;
                
                chunkLight[:,0:1,0:128] = maximum(chunkLight[:,0:1,0:128], newlight)
               
                zerochunkLight[:] = 0;
                
                
                newlight = (chunkLight[:,:,0:127]-chunkLa[:,:,1:128])
                newlight[newlight>15]=0;
                chunkLight[:,:,1:128] = maximum(chunkLight[:,:,1:128], newlight)
                
                newlight = (chunkLight[:,:,1:128]-chunkLa[:,:,0:127])
                newlight[newlight>15]=0;
                chunkLight[:,:,0:127] = maximum(chunkLight[:,:,0:127], newlight)
                zerochunkLight[:] = 0;
                
            
        timeDelta = datetime.now()-startTime;
        
        print "Completed in {0}, {1} per chunk".format(timeDelta, dirtyChunks and timeDelta/len(dirtyChunks) or 0)
        for ch in dirtyChunks:
            ch.needsLighting = False;
            
        return;
        """
            #fill all sky-facing blocks with full light 
            for x,z in itertools.product(range(16),
                                       range(16)):
                lv=15;
                hm = heightMap[z,x];
                skyLight[x,z,(hm+1)>>1:] = 255;
                
                for y in range(hm, 128):
                    activeBlocks.add((x+worldX, z+worldZ, y))
            #===================================================================
            #    for y in range(0, self.Height).__reversed__():
            #        
            #        lv-= self.materials.lightAbsorption[self.blockAt(x,y,z)];
            #            break
            #        self.setSkylightAt(x,y,z,15);
            # 
            #        activeBlocks.add( (x,z,y) )
            #===================================================================
                

        def getHeightMap(x,z):
            cx,cz = x>>4,z>>4
            x,z = x&0xf,z&0xf
            self.getChunk(cx,cz)HeightMap[z,x]

        def getLight(x,z,y):
            return self.skylightAt(x,y,z)
        def setLight(x,z,y,lv):
            return self.setSkylightAt(x,y,z,lv);

        lightAbsorption = self.materials.lightAbsorption
        
        print "Lighting %d blocks..." % len(activeBlocks);
        while len(activeBlocks):
            currentBlocks = activeBlocks;
            activeBlocks = set();
            for p in currentBlocks:
                #p = activeBlocks.pop();
                x,z,y = p
                
                hm = getHeightMap(x,z)
                
                lightValue = getLight(x,z,y);
                if y < hm: lightValue -= 1;
                
                lightValue = lightValue-lightAbsorption[self.blockAt(x,y,z)];
                setLight(x,z,y,lightValue);
                
                if lightValue:
                    try:        
                        if y<hm:
                            if(setLight(x,z,y+1, lightValue)):
                                activeBlocks.add( (x,z,y+1) )
                    except KeyError:
                        pass;

                    try:        
                        if y>0:
                            setLight(x,z,y-1, lightValue)
                            activeBlocks.add( (x,z,y-1) )
                    except KeyError:
                        pass;
                        
                    try:        
                        if(setLight(x,z+1,y, lightValue)):
                            activeBlocks.add( (x,z+1,y) )
                    except KeyError:
                        pass;
                           
                    try:        
                        if(setLight(x,z-1,y, lightValue)):
                            activeBlocks.add( (x,z-1,y) )
                    except KeyError:
                        pass;
                    
                    try:        
                        if(setLight(x+1,z,y, lightValue)):
                            activeBlocks.add( (x+1,z,y) )
                    except KeyError:
                        pass;
                    
                    try:        
                        if(setLight(x-1,z,y, lightValue)):
                            activeBlocks.add( (x-1,z,y) )
                    except KeyError:
                        pass;
                    
                #doneBlocks.add(p);

            print "Lighting pass:", len(activeBlocks);"""

    def entitiesAt(self, x, y, z):
        chunk = self.getChunk(x>>4, z>>4)
        entities = [];
        if chunk.Entities is None: return entities;
        for entity in chunk.Entities:
            if map(lambda x:int(x.value), entity[Pos]) == [x,y,z]:
                entities.append(entity);

        return entities;

    def addEntity(self, entity):
        assert isinstance(entity, TAG_Compound)
        x = int(entity[Pos][0].value)
        z = int(entity[Pos][2].value)

        chunk = self.getChunk(x>>4, z>>4)
        if not chunk:
            return None
            # raise Error, can't find a chunk?
        chunk.Entities.append(entity);
        
    def tileEntitiesAt(self, x, y, z):
        chunk = self.getChunk(x>>4, z>>4)
        entities = [];
        if chunk.TileEntities is None: return entities;
        for entity in chunk.TileEntities:
            pos = [entity[a].value for a in 'xyz']
            if pos == [x,y,z]:
                entities.append(entity);

        return entities;

    def addTileEntity(self, entity):
        assert isinstance(entity, TAG_Compound)
        x = int(entity['x'].value)
        y = int(entity['y'].value)
        z = int(entity['z'].value)

        chunk = self.getChunk(x>>4, z>>4)
        if not chunk:
            return None
            # raise Error, can't find a chunk?
        def samePosition(a):
            return (a['x'].value == x and a['y'].value == y and a['z'].value == z)
            
        try:     
            chunk.TileEntities.remove(filter(samePosition, chunk.TileEntities));
        except ValueError:
            pass;
        chunk.TileEntities.append(entity);
    
    def fillBlocks(self, box, blockType, blockData = 0):
        chunkIterator = self.getChunkSlices(box)
        
        for (chunk, slices, point) in chunkIterator:
            chunk.Blocks[slices] = blockType
            chunk.Data[slices] = blockData
            chunk.chunkChanged();
            
            
    def createChunksInRange(self, box):
        print "Creating {0} chunks in range {1}".format((box.maxcx-box.mincx)*( box.maxcz-box.mincz), ((box.mincx, box.mincz), (box.maxcx, box.maxcz)))
        for cx,cz in itertools.product(xrange(box.mincx,box.maxcx), xrange(box.mincz, box.maxcz)):
            #print cx,cz
            if not ((cx,cz) in self._presentChunks):
                #print "Making", cx, cz
                self.createChunk(cx,cz);
            assert self.containsChunk(cx,cz), "Just created {0} but it didn't take".format((cx,cz))
                
        #for cx,cz in itertools.product(xrange(minCX,maxCX), xrange(minCZ, maxCZ)):
                
    def getChunkSlices(self, box):
        """ call this method to iterate through a large slice of the world by 
            visiting each chunk and indexing its data with a subslice.
        
        this returns an iterator, which yields 3-tuples containing:
        +  an InfdevChunk object, 
        +  a x,z,y triplet of slices that can be used to index the InfdevChunk's data arrays, 
        +  a x,y,z triplet representing the relative location of this subslice within the requested world slice.
        
        
        """
        level = self
        
        #offsets of the block selection into the chunks on the edge
        minxoff, minzoff = box.minx-(box.mincx<<4), box.minz-(box.mincz<<4);
        maxxoff, maxzoff = box.maxx-(box.maxcx<<4)+16, box.maxz-(box.maxcz<<4)+16;
        
    
        for cx in range(box.mincx, box.maxcx):
            localMinX=0
            localMaxX=16
            if cx==box.mincx: 
                localMinX=minxoff
    
            if cx==box.maxcx-1:
                localMaxX=maxxoff
            newMinX = localMinX + (cx << 4) - box.minx
            newMaxX = localMaxX + (cx << 4) - box.minx
            
                            
            for cz in range(box.mincz, box.maxcz):
                localMinZ=0
                localMaxZ=16
                if cz==box.mincz: 
                    localMinZ=minzoff
                if cz==box.maxcz-1:
                    localMaxZ=maxzoff
                newMinZ = localMinZ + (cz << 4) - box.minz
                newMaxZ = localMaxZ + (cz << 4) - box.minz
                try:
                    blocks = level.blocksForChunk(cx, cz)
                except ChunkNotPresent, e:
                    #print level, "Chunk not present!", e
                    #wildChunks.add((cx,cz))
                    continue;
                #print "Chunk", cx, cz
                #print "Position in newBlocks (", newMinX, newMinZ, ")-(", newMaxX, newMaxZ, ")"
                #print "Position in chunk (", localMinX, localMinZ, ")-(", localMaxX, localMaxZ, ")"
                yield           (level.getChunk(cx, cz),
                                (slice(localMinX,localMaxX),slice(localMinZ,localMaxZ),slice(box.miny,box.maxy)),  
                                (newMinX, 0, newMinZ))
                

        
    def copyBlocksFromFinite(self, sourceLevel, sourceBox, destinationPoint, copyAir, copyWater):
        #assumes destination point and bounds have already been checked.
        (x,y,z) = destinationPoint;
        (sx, sy, sz) = sourceBox.origin
        
        filterTable = self.conversionTableFromLevel(sourceLevel);
        
        destChunks = self.getChunkSlices(BoundingBox(destinationPoint, sourceBox.size))
        for (chunk, slices, point) in destChunks:
            blocks = chunk.Blocks[slices];
            
            localSourceCorner2 = (
                sx+point[0] + blocks.shape[0],
                sy + blocks.shape[2],
                sz+point[2] + blocks.shape[1],
            )
            
            #print y, mpy
            sourceBlocks = sourceLevel.Blocks[sx+point[0]:localSourceCorner2[0],
                                              sz+point[2]:localSourceCorner2[2],
                                              sy:localSourceCorner2[1]]
            sourceBlocks = filterTable[sourceBlocks]
            
            #for small level slices, reduce the destination area
            x,z,y = sourceBlocks.shape
            blocks = blocks[0:x,0:z,0:y]
            mask = self.copyBlockArrayMasked(blocks, sourceBlocks, copyAir, copyWater)    
            
            if hasattr(sourceLevel, 'Data'):
                #infdev or schematic
                sourceData = sourceLevel.Data[sx+point[0]:localSourceCorner2[0],
                                              sz+point[2]:localSourceCorner2[2],
                                              sy:localSourceCorner2[1]]
            #
                #if isinstance(sourceLevel, MCIndevLevel):
                #    chunk.Data[slices][0:x,0:z,0:y] = sourceData[:,:,:] & 0xf #high order bits rofl
                    #chunk.Data[slices][0:x,0:z,0:y] &= 0xf
                #else:
                data = chunk.Data[slices][0:x,0:z,0:y]
                if mask != None:
                    data[mask] = (sourceData[:,:,:] & 0xf)[mask]
                else:
                    data[:] = (sourceData[:,:,:] & 0xf)
        
            chunk.chunkChanged();
                           
    def copyBlocksFrom(self, sourceLevel, sourceBox, destinationPoint, copyAir = True, copyWater = True):
        (x,y,z) = destinationPoint;
        (lx,ly,lz) = sourceBox.size
        #sourcePoint, sourcePoint1 = sourceBox
        
        if y<0: 
            sourceBox.origin[1] -=y
            y = 0;
        if y+ly>self.Height:
            sourceBox.size[1] -=y+ly-self.Height
            y=self.Height-ly
        
        destinationPoint = (x,y,z)
        #needs work xxx
        print "Copying {0} blocks from {1} to {2}" .format (ly*lz*lx,sourceBox, destinationPoint)
        blocksCopied = 0
        
        if(not isinstance(sourceLevel, MCInfdevOldLevel)):
            self.copyBlocksFromFinite(sourceLevel, sourceBox, destinationPoint, copyAir, copyWater)
            

        else: #uggh clone tool will still be slow if it weren't for schematics
            filterTable = sourceLevel.materials.conversionTables[self.materials]
            copyOffset = map(lambda x,y:x-y, destinationPoint, sourceBox.origin)
            for s in itertools.product(*map(lambda x:range(*x), zip(sourceBox.origin, sourceBox.size))):
               destX, destZ, destY = copyOffset[0]+s[0], copyOffset[2]+s[2], copyOffset[1]+s[1]
               
               destChunkX,destChunkZ = destX>>4,destZ>>4
               destBlockX = destX & 0xf
               destBlockZ = destZ & 0xf
               
               try:
                   chunk = self.getChunk( destChunkX,destChunkZ )
                   blocks=chunk.Blocks
                   
                   blockType = sourceLevel.blockAt(*s)
                   blockType = filterTable[blockType]
                   if blockType == 0 and not copyAir: continue
                   if blockType == 8 or blockType == 9 and not copyWater: continue;
                   
                   blocks[destBlockX,destBlockZ,destY] = blockType
                   self.setBlockDataAt(destX, destY, destZ, sourceLevel.blockDataAt(*s))
                           
               except ChunkNotPresent, e:
                   continue;
               else:
                   chunk.chunkChanged();
                   blocksCopied += 1;
#""">>>>>>> cfe22db... improved block copying speed"""

        self.copyEntitiesFrom(sourceLevel, sourceBox, destinationPoint)
        print "Blocks copied: %d" % blocksCopied;
        #self.saveInPlace()
 

    def containsPoint(self, x, y, z):
        if y<0 or y>127: return False;
        return self.containsChunk(x>>4, z>>4)
    
    def containsChunk(self, cx, cz):
        return (cx, cz) in self._presentChunks;
        #return c.ready();

    def malformedChunk(self, cx, cz):
        print "Chunk {0} malformed ({1})".format((cx,cz), self.chunkFilename(cx,cz))
        del self._presentChunks[(cx,cz)]
        
    def createChunk(self, cx, cz):
        if (cx,cz) in self._presentChunks: raise ValueError, "{0}:Chunk {1} already present!".format(self, (cx,cz) )
        self._presentChunks[cx,cz] = InfdevChunk(self, (cx,cz), create = True)
        
        
        
    def deleteChunk(self, cx, cz):
        if not (cx,cz) in self._presentChunks: return;
        self._presentChunks[(cx,cz)].remove();
        
        del self._presentChunks[(cx,cz)]
        
        
    def setPlayerSpawnPosition(self, pos):
        xyz = ["SpawnX", "SpawnY", "SpawnZ"]
        for name, val in zip(xyz, pos):
            self.root_tag["Data"][name] = nbt.TAG_Int(val);

        #self.saveInPlace();

    def playerSpawnPosition(self):
        xyz = ["SpawnX", "SpawnY", "SpawnZ"]
        return array([self.root_tag["Data"][i].value for i in xyz])
   
    def setPlayerPosition(self, pos):
        self.root_tag["Data"]["Player"]["Pos"] = nbt.TAG_List([nbt.TAG_Double(p) for p in pos])

    def playerPosition(self):
        pos = map(lambda x:x.value, self.root_tag["Data"]["Player"]["Pos"]);
        return array(pos);
    
    def setPlayerOrientation(self, yp):
        self.root_tag["Data"]["Player"]["Rotation"] = nbt.TAG_List([nbt.TAG_Float(p) for p in yp])
    
    def playerOrientation(self):
        """ returns (yaw, pitch) """
        yp = map(lambda x:x.value, self.root_tag["Data"]["Player"]["Rotation"]);
        y,p = yp;
        if p==0: p=0.000000001;
        if p==180.0:  p-=0.000000001;
        yp = y,p;
        return array(yp);

    
class MCIndevLevel(MCLevel):
    
    """ IMPORTANT: self.Blocks and self.Data are indexed with [y,z,x]
    because that's how the array appears"""
    #def neighborsAndBlock(self, x, y, z):
##    def blocksForChunk(self, cx, cz):
##        return self.Blocks[:,
##                           cz*self.chunkSize:cz*self.chunkSize+self.chunkSize,
##                           cx*self.chunkSize:cx*self.chunkSize+self.chunkSize]
##

    def setPlayerSpawnPosition(self, pos):
        assert len(pos) == 3
        self.Spawn = array(pos);

    def playerSpawnPosition(self):
        return self.Spawn;
        
    def setPlayerPosition(self, pos):
        for x in self.root_tag["Entities"]:
            if x["id"].value == "LocalPlayer":
                x["Pos"] = nbt.TAG_List([nbt.TAG_Float(p) for p in pos])
    
    def playerPosition(self):
        for x in self.root_tag["Entities"]:
            if x["id"].value == "LocalPlayer":
                return array(map(lambda x:x.value, x["Pos"]));
                
    def setPlayerOrientation(self, yp):
        for x in self.root_tag["Entities"]:
            if x["id"].value == "LocalPlayer":
                x["Rotation"] = nbt.TAG_List([nbt.TAG_Float(p) for p in yp])

    def playerOrientation(self):
        """ returns (yaw, pitch) """
        for x in self.root_tag["Entities"]:
            if x["id"].value == "LocalPlayer":
                return array(map(lambda x:x.value, x["Rotation"]));
    
    def setBlockDataAt(self, x,y,z, newdata):
        if x<0 or y<0 or z<0: return 0
        if x>=self.Width or y>=self.Height or z>=self.Length: return 0;
        self.Data[x,z,y] &= 0xf0
        self.Data[x,z,y] |= (newdata & 0xf);        

    def blockDataAt(self, x, y, z):
        if x<0 or y<0 or z<0: return 0
        if x>=self.Width or y>=self.Height or z>=self.Length: return 0;
        return self.Data[x,z,y] & 0xf;
    
    def blockLightAt(self, x, y, z):
        if x<0 or y<0 or z<0: return 0
        if x>=self.Width or y>=self.Height or z>=self.Length: return 0;
        return (self.Data[x,z,y] & 0xf);
    
    def __repr__(self):
        return "MCIndevLevel({0}): {1}W {2}L {3}H".format(self.filename, self.Width, self.Length, self.Height)
    def __init__(self, root_tag = None, filename = ""):
        self.Width = 0
        self.Height = 0
        self.Length = 0
        self.Blocks = array([], uint8)
        self.Data = array([], uint8)
        self.Spawn = (0,0,0)
        self.filename = filename;
        
        
        if root_tag:
        
            self.root_tag = root_tag;
            mapTag = root_tag[Map];
            self.Width = mapTag[Width].value
            self.Length = mapTag[Length].value
            self.Height = mapTag[Height].value
            self.Blocks = mapTag[Blocks].value
            self.Blocks.shape = (self.Width, self.Length, self.Height,  )
            self.oldBlockStrides = self.Blocks.strides
            self.Blocks.strides = (1, self.Width, self.Width * self.Length)

            self.Data = mapTag[Data].value
            self.Data.shape = (self.Width, self.Length, self.Height,  )
            self.oldDataStrides = self.Data.strides
            self.Data.strides = (1, self.Width, self.Width * self.Length)
            
            self.Spawn = [mapTag[Spawn][i].value for i in range(3)];
            
            if not Entities in root_tag: 
                root_tag[Entities] = TAG_List();
            self.Entities = root_tag[Entities]

            if not TileEntities in root_tag: 
                root_tag[TileEntities] = TAG_List();
            self.TileEntities = root_tag[TileEntities]
            

            if len(filter(lambda x:x['id'].value=='LocalPlayer', root_tag[Entities])) == 0: #omen doesn't make a player entity
                p=TAG_Compound()
                p['id'] = TAG_String('LocalPlayer')
                p['Pos'] = TAG_List([TAG_Float(0.), TAG_Float(64.), TAG_Float(0.)])
                p['Rotation'] = TAG_List([TAG_Float(0.), TAG_Float(45.)])
                
                root_tag[Entities].append(p)
                #self.saveInPlace();
                
        else:
            print "New Level!";
            raise ValueError, "Can't do that sir"
#            self.SurroundingGroundHeight = root_tag[Environment][SurroundingGroundHeight].value
#            self.SurroundingGroundType = root_tag[Environment][SurroundingGroundType].value
#            self.SurroundingWaterHeight = root_tag[Environment][SurroundingGroundHeight].value
#            self.SurroundingWaterType = root_tag[Environment][SurroundingWaterType].value
#            self.CloudHeight = root_tag[Environment][CloudHeight].value
#            self.CloudColor = root_tag[Environment][CloudColor].value
#            self.SkyColor = root_tag[Environment][SkyColor].value
#            self.FogColor = root_tag[Environment][FogColor].value
#            self.SkyBrightness = root_tag[Environment][SkyBrightness].value
#            self.TimeOfDay = root_tag[Environment]["TimeOfDay"].value
#
#              
#            self.Name = self.root_tag[About][Name].value
#            self.Author = self.root_tag[About][Author].value
#            self.CreatedOn = self.root_tag[About][CreatedOn].value

                    
    
    def rotateLeft(self):
        MCLevel.rotateLeft(self);
        
        self.Data = swapaxes(self.Data, 1, 0)[:,::-1,:]; #x=y; y=-x
        
        torchRotation = array([0, 4, 3, 1, 2, 5,
                               6, 7, 
                               
                               8, 9, 10, 11, 12, 13, 14, 15]);
                               
        torchIndexes = (self.Blocks == self.materials.materialNamed("Torch"))
        print "Rotating torches: ", len(torchIndexes.nonzero()[0]);
        self.Data[torchIndexes] = torchRotation[self.Data[torchIndexes]]
        
        
    def saveToFile(self, filename = None):
        if filename == None: filename = self.filename;
        if filename == None:
            print "Attempted to save an unnamed schematic in place :x"
            return; #you fool!
        
        newBlockStrides = self.Blocks.strides;
        self.Blocks.strides = self.oldBlockStrides;
        newDataStrides = self.Data.strides;
        self.Data.strides = self.oldDataStrides;

        mapTag = nbt.TAG_Compound( name=Map );
        mapTag[Width] = nbt.TAG_Short(self.Width);
        mapTag[Height] = nbt.TAG_Short(self.Height);
        mapTag[Length] = nbt.TAG_Short(self.Length);
        mapTag[Blocks] = nbt.TAG_Byte_Array(self.Blocks);
        mapTag[Data]   = nbt.TAG_Byte_Array(self.Data);
        mapTag[Spawn]  = nbt.TAG_List([nbt.TAG_Short(i) for i in self.Spawn])

        self.root_tag[Map] = mapTag;
        self.root_tag[Map]
        #output_file = gzip.open(self.filename, "wb", compresslevel=1)
        try:
            os.rename(filename, filename + ".old");
        except Exception,e:
            #print "Atomic Save: No existing file to rename"
            pass
            
        try:
            self.root_tag.saveGzipped(filename);
        except:
            os.rename(filename + ".old", filename);
            
        try: os.remove(filename + ".old");
        except Exception,e:
            #print "Atomic Save: No old file to remove"
            pass
        
        self.Blocks.strides = newBlockStrides;
        self.Data.strides = newDataStrides;
         

import re

class MCSharpLevel(MCLevel):
    """ int magic = convert(data.readShort());
        logger.trace("Magic number: {}", magic);
        if (magic != 1874)
            throw new IOException("Only version 1 MCSharp levels supported (magic number was "+magic+")");

        int width = convert(data.readShort());
        int height = convert(data.readShort());
        int depth = convert(data.readShort());
        logger.trace("Width: {}", width);
        logger.trace("Depth: {}", depth);
        logger.trace("Height: {}", height);

        int spawnX = convert(data.readShort());
        int spawnY = convert(data.readShort());
        int spawnZ = convert(data.readShort());

        int spawnRotation = data.readUnsignedByte();
        int spawnPitch = data.readUnsignedByte();

        int visitRanks = data.readUnsignedByte();
        int buildRanks = data.readUnsignedByte();

        byte[][][] blocks = new byte[width][height][depth];
        int i = 0;
        BlockManager manager = BlockManager.getBlockManager();
        for(int z = 0;z<depth;z++) {
            for(int y = 0;y<height;y++) {
                byte[] row = new byte[height];
                data.readFully(row);
                for(int x = 0;x<width;x++) {
                    blocks[x][y][z] = translateBlock(row[x]);
                }
            }
        }

        lvl.setBlocks(blocks, new byte[width][height][depth], width, height, depth);
        lvl.setSpawnPosition(new Position(spawnX, spawnY, spawnZ));
        lvl.setSpawnRotation(new Rotation(spawnRotation, spawnPitch));
        lvl.setEnvironment(new Environment());

        return lvl;
    }"""
    
class MCJavaLevel(MCLevel):
    def setBlockDataAt(self, *args): pass
    def blockDataAt(self, *args): return 0;
    
    def guessSize(self, data):
        if(data.shape[0] <= (32 * 32 * 64)*2):
            print "Tiny map is too small!";
            raise IOError, "MCJavaLevel attempted for smaller than 64 blocks cubed"
        if(data.shape[0] > (32 * 32 * 64)*2):
            self.Width = 64
            self.Length = 64
            self.Height = 64
        if(data.shape[0] > (64 * 64 * 64)*2):
            self.Width = 128
            self.Length = 128
            self.Height = 64
        if(data.shape[0] > (128 * 128 * 64)*2):
            self.Width = 256
            self.Length = 256
            self.Height = 64
        if(data.shape[0] > (256 * 256 * 64)*2): #could also be 256*256*256
            self.Width = 256
            self.Length = 256
            self.Height = 256
        if(data.shape[0] > 512 * 512 * 64 * 2): # just to load shadowmarch castle
            self.Width = 512
            self.Length = 512
            self.Height = 256
            
    def __init__(self, data, filename):
        self.filename = filename;
        self.filedata = data;
        #try to take x,z,y from the filename
        r=re.search('(\d+).*?(\d+).*?(\d+)', filename)
        if r and len(r.groups()) == 3:
            (w, l, h) = map(int, r.groups())
            if w*l*h <= data.shape[0]:
                (self.Width, self.Length, self.Height) = w,l,h
            else:
                self.guessSize(data);
        else:
            self.guessSize(data);
            
        print "MCJavaLevel created for potential level of size ", (self.Width, self.Length, self.Height) 
            
        blockCount = self.Height * self.Length * self.Width
        if blockCount > data.shape[0]: raise ValueError, "Level file does not contain enough blocks!"
        
        blockOffset = data.shape[0]-blockCount
        blocks = data[blockOffset:blockOffset+blockCount]
        #print blockOffset, blockCount, len(blocks);
        maxBlockType = 64 #maximum allowed in classic
        while(max(blocks[-4096:]) > maxBlockType):
            #guess the block array by starting at the end of the file
            #and sliding the blockCount-sized window back until it
            #looks like every block has a valid blockNumber
            blockOffset -=1;
            blocks = data[blockOffset:blockOffset+blockCount]
        
            if blockOffset <= -data.shape[0]:
                raise IOError, "Can't find a valid array of blocks <= #%d" % maxBlockType
        
        self.Blocks = blocks;
        self.blockOffset = blockOffset;
        blocks.shape = (self.Width,self.Length,self.Height, );
        blocks.strides = (1, self.Width, self.Width * self.Length);

            
    def saveInPlace(self):
        #f = file(self.filename, 'rb')
        #filedata = f.read()
        #f.close();
        
##        
##        blockstr = self.Blocks.tostring()
##        firstdata = filedata[0:self.blockOffset]
##        lastdata = filedata[self.blockOffset+len(blockstr):];

        s = StringIO.StringIO()
        #print "COMPRESSED?", self.compressed
        if self.compressed:
            g = gzip.GzipFile(fileobj=s, mode='wb');
        else:
            g = s;
##            g.write(firstdata);
##            g.write(blockstr);
##            g.write(lastdata);
        g.write(self.filedata.tostring());
        g.flush();
        g.close()

        try:
            os.rename(self.filename, self.filename + ".old");
        except Exception,e:
            #print "Atomic Save: No existing file to rename"
            pass;
        
        try:        
            f = file(self.filename, 'wb')
            f.write(s.getvalue());
            
        except Exception, e:
            print "Error while saving java level in place: ", e
            f.close()
            try:os.remove(self.filename);
            except: pass
            os.rename(self.filename + ".old", self.filename);

        try:
            os.remove(self.filename + ".old");
        except Exception,e:
            #print "Atomic Save: No old file to remove"
            pass;
        f.close()
            
###xxxxx CHECK RESULTS
def testJavaLevels():
    print "Java level"
    indevlevel = MCLevel.fromFile("hell.mclevel")
    
    creativelevel = MCLevel.fromFile("bigshadowmarch.mine");
    creativelevel.blocksForChunk(0,0);
    creativelevel.copyBlocksFrom(indevlevel, BoundingBox((0,0,0), (64,64,64,)), (0,0,0) )
    assert(all(indevlevel.Blocks[0:64,0:64,0:64] == creativelevel.Blocks[0:64,0:64,0:64])) 
    
    creativelevel.saveInPlace()
    #xxx old survival levels

def testIndevLevels():
    print "Indev level"
    
    srclevel = MCLevel.fromFile("hell.mclevel")
    indevlevel = MCLevel.fromFile("hueg.mclevel")
    indevlevel.blocksForChunk(0,0);
    indevlevel.copyBlocksFrom(srclevel, BoundingBox((0,0,0), (64,64,64,)), (0,0,0) ) 
    assert(all(indevlevel.Blocks[0:64,0:64,0:64] == srclevel.Blocks[0:64,0:64,0:64])) 
    indevlevel.saveInPlace()
    
def testAlphaLevels():
    print "Alpha level"
    indevlevel = MCLevel.fromFile("hell.mclevel")
    
    level = MCInfdevOldLevel(filename="d:\Testworld");
    for ch in level.presentChunks: level.deleteChunk(*ch)
    level.createChunksInRange( BoundingBox((0,0,0), (32, 0, 32)) )
    level.copyBlocksFrom(indevlevel, BoundingBox((0,0,0), (256, 128, 256)), (-0, 0, 0)) 
    assert all(level.getChunk(0,0).Blocks[0:16,0:16,0:indevlevel.Height] == indevlevel.conversionTableFromLevel(level)[indevlevel.Blocks[0:16,0:16,0:indevlevel.Height]]);
    
    schem = MCLevel.fromFile(os.path.expandvars("schematics\\CreativeInABox.schematic"));
    level.copyBlocksFrom(schem, BoundingBox((0,0,0), (1,1,3)), (0, 64, 0));
    schem = MCSchematic( shape=(1,1,3) )
    schem.copyBlocksFrom(level, BoundingBox((0, 64, 0), (1, 1, 3)), (0,0,0));
    assert all(level.getChunk(0,0).Blocks[0:1,0:3,64:65] == schem.conversionTableFromLevel(level)[schem.Blocks])
    
    try:
        for x,z in itertools.product(xrange(-1,3),xrange(-1,2)):
            level.deleteChunk(x, z);
            level.createChunk(x, z)
    except Exception, e:
        traceback.print_exc();
        print e;
    level.fillBlocks( BoundingBox((-11, 0, -7), (38, 128, 25)) , 5);
    c = level.getChunk( 0, 0)
    assert all(c.Blocks == 5)
    #print b.shape
    #raise SystemExit
    cx, cz = -3,-1;
    
    try:
        level.deleteChunk(cx, cz);
    except KeyError:pass
    level.createChunk(cx, cz);
    level.copyBlocksFrom(indevlevel, BoundingBox((0,0,0), (64,64,64,)), (-96, 32, 0)) 
    #blocks = zeros((16,16,128), 'uint8');
    #blocks[:,:,:] = level.getChunk(cx, cz).Blocks[:,:,:]
    #level.getChunk(cx, cz).Blocks[:,:,:] = blocks[:,:,:]
    level.generateLights();
    level.saveInPlace();
    
    level.saveInPlace();
    
    
def testSchematics():
    print "Schematic from indev"
    
    size=(64,64,64)
    schematic = MCSchematic(shape=size, filename = "hell.schematic", mats='Classic');
    level = MCLevel.fromFile("hell.mclevel")
    schematic.rotateLeft();
    try:
        schematic.copyBlocksFrom(level, BoundingBox((-32,-32,-32), (64,64,64,)), (0,0,0) )
    except ValueError:
        pass;
    
    
    schematic.copyBlocksFrom(level, BoundingBox((0,0,0), (64,64,64,)), (0,0,0) )
    assert(all(schematic.Blocks[0:64,0:64,0:64] == level.Blocks[0:64,0:64,0:64])) 
    schematic.compress();
    
    schematic.copyBlocksFrom(level, BoundingBox((0,0,0), (64,64,64,)), (-32, -32, -32))
    assert(all(schematic.Blocks[0:32,0:32,0:32] == level.Blocks[32:64,32:64,32:64])) 
    
    schematic.compress();
    
    schematic.saveInPlace();
    
    schem = MCLevel.fromFile(os.path.expandvars("schematics\CreativeInABox.schematic"));
    tempSchematic = MCSchematic(shape=(1,1,3))
    tempSchematic.copyBlocksFrom(schem, BoundingBox((0,0,0), (1,1,3)), (0,0,0))
    
    print "Schematic from alpha"
    level = MCLevel.fromFile(os.path.expandvars("%APPDATA%\.minecraft\saves\World1\level.dat"));
    for cx,cz in itertools.product(xrange(0, 4), xrange(0, 4) ):
        try:
            level.createChunk(cx,cz)
        except ValueError:
            pass
    schematic.copyBlocksFrom(level, BoundingBox((0,0,0), (64,64,64,)), (0,0,0) )
    
                             
def testmain():
    testSchematics();
    testIndevLevels();
    testAlphaLevels();
    testJavaLevels();
    
#import cProfile   
if __name__=="__main__":
    #cProfile.run('testmain()');
    testmain();
